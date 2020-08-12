from janggu.data import Bioseq, Cover
from pybedtools import BedTool

import sys
from sklearn.preprocessing import LabelEncoder
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
import pandas as pd
import numpy as np

from sklearn import metrics, calibration

#from NN_utils import *

def to_np(tensor):
    if torch.cuda.is_available():
        return tensor.cpu().detach().numpy()
    else:
        return tensor.detach().numpy()

def seq2ohe(sequence,motlen):
    rows = len(sequence)+2*motlen-2
    S = np.empty([rows,4])
    base = 'ACGT'
    for i in range(rows):
        for j in range(4):
            if i-motlen+1<len(sequence) and sequence[i-motlen+1].upper() =='N' or i<motlen-1 or i>len(sequence)+motlen-2:
                S[i,j]=np.float32(0.25)
            elif sequence[i-motlen+1].upper() == base[j]:
                S[i,j]=np.float32(1)
            else:
                S[i,j]=np.float32(0)
    return np.transpose(S)

def seqs2ohe(sequences,motiflen=24):

    dataset=[]
    for row in sequences:             
        dataset.append(seq2ohe(row,motiflen))
        
  
    return dataset

class seqDataset(Dataset):
    """ Diabetes dataset."""

    def __init__(self,xy=None):
        #self.x_data = np.asarray([el for el in xy[0]],dtype=np.float32)
        self.x_data = np.asarray(xy[0], dtype=np.float32)
        #self.y_data = np.asarray([el for el in xy[1]],dtype=np.float32)
        self.y_data = np.asarray(xy[1], dtype=np.float32)
        
        self.x_data = torch.from_numpy(self.x_data)
        self.y_data = torch.from_numpy(self.y_data)
        
        self.len=len(self.x_data)


    def __getitem__(self, index):
        return self.x_data[index], self.y_data[index]

    def __len__(self):
        return self.len

class CombinedDataset(Dataset):
    """ Combined dataset."""

    def __init__(self,local_dataset, distal_dataset):
        
        self.y = local_dataset.y
        self.local_cont_X = local_dataset.cont_X
        self.local_cat_X = local_dataset.cat_X
        self.distal_X = distal_dataset.x_data
        self.len=len(self.y)

    def __getitem__(self, index):
        return self.y[index], self.local_cont_X[index], self.local_cat_X[index], self.distal_X[index]

    def __len__(self):
        return self.len
    
def gen_ohe_dataset(data):
    seq_data = data['seq']
    y_data = data['mut_type'].astype(np.float32).values.reshape(-1, 1)

    seqs_ohe = seqs2ohe(seq_data, motiflen=6)

    dataset = seqDataset([seqs_ohe, y_data])
    #print(dataset[0:2][0][0][0:4,4:10])
    
    return dataset
    
def separate_local_distal(data, radius = 5): 
    seq_len = len(data['seq'][0])
    mid_pos = int((seq_len+1)/2)

    adj_seq = pd.DataFrame([list(el[mid_pos-(radius+1):mid_pos+radius]) for el in data['seq']])
    adj_seq.columns = ['us'+str(radius - i)for i in range(radius)] + ['mid'] + ['ds'+str(i+1)for i in range(radius)]

    #local sequences and functional genomic data
    data_local = pd.concat([adj_seq, data.drop(['pos','seq'], axis=1)], axis=1)

    #consider more distal sequences
    data_distal = data[['seq', 'mut_type']]
    
    categorical_features = list(adj_seq.columns)
    #categorical_features = ["us5", "us4", "us3", "us2", "us1", "ds1", "ds2", "ds3", "ds4", "ds5"]
    
    return data_local, data_distal, categorical_features

    
class TabularDataset(Dataset):
    def __init__(self, data, cat_cols, output_col):
        """
        Characterizes a Dataset for PyTorch

        Parameters
        ----------

        data: pandas data frame
            The data frame object for the input data. It must
            contain all the continuous, categorical and the
            output columns to be used.

        cat_cols: List of strings
            The names of the categorical columns in the data.
            These columns will be passed through the embedding
            layers in the model. These columns must be
            label encoded beforehand. 

        output_col: string
            The name of the output variable column in the data
            provided.
        """
        #first, change labels to digits
        label_encoders = {}
        for cat_col in cat_cols:
            label_encoders[cat_col] = LabelEncoder()
            data[cat_col] = label_encoders[cat_col].fit_transform(data[cat_col])
        
        self.n = data.shape[0]

        if output_col:
            self.y = data[output_col].astype(np.float32).values.reshape(-1, 1)
        else:
            self.y = np.zeros((self.n, 1))

        self.cat_cols = cat_cols
        self.cont_cols = [col for col in data.columns if col not in self.cat_cols + [output_col]]

        if self.cont_cols:
            self.cont_X = data[self.cont_cols].astype(np.float32).values
        else:
            self.cont_X = np.zeros((self.n, 1))

        if len(self.cat_cols) >0:
            self.cat_X = data[cat_cols].astype(np.int64).values
        else:
            self.cat_X =    np.zeros((self.n, 1))

    def __len__(self):
        """
        Denotes the total number of samples.
        """
        return self.n

    def __getitem__(self, idx):
        """
        Generates one sample of data.
        """
        return [self.y[idx], self.cont_X[idx], self.cat_X[idx]]


def prepare_dataset(bed_regions, ref_genome,  bw_files, bw_names, radius=5, distal_radius=50, distal_order=1):

    local_seq = Bioseq.create_from_refgenome(name='', refgenome=ref_genome, roi=bed_regions, flank=radius)

    #get the numberized seq data
    local_seq_cat = local_seq.iseq4idx(list(range(local_seq.shape[0])))

    #TO DO: some other categorical data can be added here

    categorical_features = ['us'+str(radius - i)for i in range(radius)] + ['mid'] + ['ds'+str(i+1)for i in range(radius)]

    local_seq_cat = pd.DataFrame(local_seq_cat, columns = categorical_features)

    #adj_seq.columns = ['us'+str(radius - i)for i in range(radius)] + ['mid'] + ['ds'+str(i+1)for i in range(radius)]

    #
    y = np.array([float(loc.score) for loc in bed_regions], ndmin=2).reshape((-1,1))
    y = pd.DataFrame(y, columns=['mut_type'])
    output_feature = 'mut_type'

    bw_data = np.array(Cover.create_from_bigwig(name='', bigwigfiles=bw_files, roi=bed_regions, resolution=2*radius+1, flank=radius)).reshape(len(bed_regions), -1)

    bw_data = pd.DataFrame(bw_data, columns=bw_names)
    #print ('bw_data.shape', bw_data.shape, local_seq_cat.shape)

    data_local = pd.concat([local_seq_cat, bw_data, y], axis=1)

    dataset_local = TabularDataset(data=data_local, cat_cols=categorical_features, output_col=output_feature)

    #######

    distal_seq = Bioseq.create_from_refgenome(name='distal', refgenome=ref_genome, roi=bed_regions, flank=distal_radius, order=distal_order)

    distal_seq = np.array(distal_seq).squeeze().transpose(0,2,1)
    dataset_distal = seqDataset([distal_seq, y])

    dataset = CombinedDataset(dataset_local, dataset_distal)
    
    return dataset, data_local, categorical_features

#old function
def load_data(data_file):
    
    data = pd.read_csv(data_file, sep='\t').dropna()
    seq_data = data['sequence']
    y_data = data['label'].astype(np.float32).values.reshape(-1, 1)

    seqs_ohe = seqs2ohe(seq_data, 6)

    dataset = seqDataset([seqs_ohe, y_data])
    #print(dataset[0:2][0][0][0:4,4:10])
    
    return dataset




