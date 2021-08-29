import warnings
warnings.filterwarnings('ignore',category=FutureWarning)

from pybedtools import BedTool

import sys
import argparse
import textwrap

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
import pandas as pd
import numpy as np
import pickle

import os
import time
import datetime

from MuRaL.nn_models import *
from MuRaL.nn_utils import *
from MuRaL.preprocessing import *
from MuRaL.evaluation import *
from MuRaL.training import *

from pynvml import *

from functools import partial
import ray
from ray import tune
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.allow_tf32 = True

def parse_arguments(parser):
    """
    Parse parameters from the command line
    """
    optional = parser._action_groups.pop()
    required = parser.add_argument_group('required arguments')
        


    required.add_argument('--model_path', type=str, metavar='FILE', required=True,
                          help=textwrap.dedent("""
                          File path of the trained model.
                          """ ).strip())  
    
    
    required.add_argument('--ref_genome', type=str, metavar='FILE', default='',  
                          required=True, help=textwrap.dedent("""
                          File path of the reference genome in FASTA format.""").strip())
    
    required.add_argument('--train_data', type=str, metavar='FILE', default='',  
                          required=True, help= textwrap.dedent("""
                          File path of training data in a sorted BED format. If the options
                          --validation_data and --valid_ratio not specified, 10%% of the
                          sites sampled from the training BED will be used as the
                          validation data.""").strip())
    
    optional.add_argument('--model_config_path', type=str, metavar='FILE', 
                          help=textwrap.dedent("""
                          File path for the paired configurations of the trained model.
                          """ ).strip())
    
    optional.add_argument('--train_all', default=False, action='store_true')
    
    optional.add_argument('--init_fc_with_pretrained', default=False, action='store_true')  
    
    optional.add_argument('--validation_data', type=str, metavar='FILE', default=None,
                          help=textwrap.dedent("""
                          File path for validation data. If this option is set,
                          the value of --valid_ratio will be ignored. Default: None.
                          """).strip()) 
    
    optional.add_argument('--bw_paths', type=str, metavar='FILE', default=None,
                          help=textwrap.dedent("""
                          File path for a list of BigWig files for non-sequence 
                          features such as the coverage track. Default: None.""").strip())
    
    optional.add_argument('--seq_only', default=False, action='store_true', 
                          help=textwrap.dedent("""
                          If set, use only genomic sequences for the model and ignore
                          bigWig tracks. Default: False.""").strip())
    
    optional.add_argument('--n_class', type=int, metavar='INT', default='4',  
                          help=textwrap.dedent("""
                          Number of mutation classes (or types), including the 
                          non-mutated class. Default: 4.""").strip())
    
    optional.add_argument('--local_radius', type=int, metavar='INT', default=5, 
                          help=textwrap.dedent("""
                          Radius of the local sequence to be considered in the 
                          model. Length of the local sequence = local_radius*2+1 bp.
                          Default: 5.""" ).strip())
    
    optional.add_argument('--local_order', type=int, metavar='INT', default=3, 
                          help=textwrap.dedent("""
                          Length of k-mer in the embedding layer. Default: 3.""").strip())
    
    optional.add_argument('--local_hidden1_size', type=int, metavar='INT', default=150,
                          help=textwrap.dedent("""
                          Size of 1st hidden layer for local module. Default: 150.
                          """).strip())
    
    optional.add_argument('--local_hidden2_size', type=int, metavar='INT', default=0,
                          help=textwrap.dedent("""
                          Size of 2nd hidden layer for local module. 
                          Default: local_hidden1_size//2 .
                          """ ).strip())
    
    optional.add_argument('--distal_radius', type=int, metavar='INT', default=50,
                          help=textwrap.dedent("""
                          Radius of the expanded sequence to be considered in the model. 
                          Length of the expanded sequence = distal_radius*2+1 bp.
                          Default: 50. 
                          """ ).strip())
    
    optional.add_argument('--distal_order', type=int, metavar='INT', default=1, 
                          help=textwrap.dedent("""
                          Order of distal sequences to be considered. Kept for 
                          future development. Default: 1. """ ).strip())
        
    optional.add_argument('--batch_size', type=int, metavar='INT', default=128, 
                          help=textwrap.dedent("""
                          Size of mini batches for training. Default: 128.
                          """ ).strip())
    
    optional.add_argument('--emb_dropout', type=float, metavar='FLOAT', default=0.1, 
                          help=textwrap.dedent("""
                          Dropout rate for inputs of the k-mer embedding layer.
                          Default: 0.1.""" ).strip())
    
    optional.add_argument('--local_dropout', type=float, metavar='FLOAT', default=0.1, 
                          help=textwrap.dedent("""
                          Dropout rate for inputs of local hidden layers.  Default: 0.1.""" ).strip())
    
    optional.add_argument('--CNN_kernel_size', type=int, metavar='INT', default=3, 
                          help=textwrap.dedent("""
                          Kernel size for CNN layers in the expanded module. Default: 3.
                          """ ).strip())
    
    optional.add_argument('--CNN_out_channels', type=int, metavar='INT', default=32, 
                          help=textwrap.dedent("""
                          Number of output channels for CNN layers. Default: 32.
                          """ ).strip())
    
    optional.add_argument('--distal_fc_dropout', type=float, metavar='FLOAT', default=0.25, 
                          help=textwrap.dedent("""
                          Dropout rate for the FC layer of the expanded module.
                          Default: 0.25.
                           """ ).strip())
    
    
    optional.add_argument('--model_no', type=int, metavar='INT', default=2, 
                          help=textwrap.dedent("""
                          Which network architecture to be used: 
                          0 - 'local-only' model;
                          1 - 'expanded-only' model;
                          2 - 'local + expanded' model. 
                          Default: 2.
                          """ ).strip())
    
                          
    optional.add_argument('--optim', type=str, metavar='STRING', default=['Adam'], nargs='+', 
                          help=textwrap.dedent("""
                          Name of optimization method for learning.
                          Default: 'Adam'.
                          """ ).strip())
    
    optional.add_argument('--cuda_id', type=str, metavar='STRING', default='0', 
                          help=textwrap.dedent("""
                          Which GPU device to be used. Default: '0'. 
                          """ ).strip())
    
    optional.add_argument('--valid_ratio', type=float, metavar='FLOAT', default=0.1, 
                          help=textwrap.dedent("""
                          Ratio of validation data relative to the whole training data.
                          Default: 0.1. 
                          """ ).strip())
    
    optional.add_argument('--split_seed', type=int, metavar='INT', default=-1, 
                          help=textwrap.dedent("""
                          Seed for randomly splitting data into training and validation
                          sets. Default: a random number generated by the job.
                          """ ).strip())
    
    optional.add_argument('--learning_rate', type=float, metavar='FLOAT', default=[0.005], nargs='+', 
                          help=textwrap.dedent("""
                          Learning rate for network training, a parameter for the 
                          optimization method.  Default: 0.005.
                          """ ).strip())
    
    optional.add_argument('--weight_decay', type=float, metavar='FLOAT', default=[1e-5], nargs='+', 
                          help=textwrap.dedent("""
                          'weight_decay' parameter (regularization) for the optimization 
                          method.  Default: 1e-5. 
                          """ ).strip())
    
    optional.add_argument('--LR_gamma', type=float, metavar='FLOAT', default=[0.5], nargs='+', 
                          help=textwrap.dedent("""
                          'gamma' parameter for the learning rate scheduler.
                           Default: 0.5.
                           """ ).strip())
    
    optional.add_argument('--epochs', type=int, metavar='INT', default=10, 
                          help=textwrap.dedent("""
                          Maximum number of epochs for each trial.  Default: 10.
                          """ ).strip())
    
    optional.add_argument('--grace_period', type=int, metavar='INT', default=5, 
                          help=textwrap.dedent("""
                          'grace_period' parameter for early stopping. 
                           Default: 5.
                           """ ).strip())
    
    
    optional.add_argument('--n_trials', type=int, metavar='INT', default=3, 
                          help=textwrap.dedent("""
                          Number of trials for this training job.  Default: 3.
                          """ ).strip())
    
    optional.add_argument('--experiment_name', type=str, metavar='STRING', default='my_experiment',
                          help=textwrap.dedent("""
                          Ray-Tune experiment name.  Default: 'my_experiment'.
                          """ ).strip())
    
    optional.add_argument('--ASHA_metric', type=str, metavar='STRING', default='loss', 
                          help=textwrap.dedent("""
                          Metric for ASHA schedualing; the value can be 'loss' or 'score'.
                          Default: 'loss'.
                          """ ).strip())
    
    optional.add_argument('--ray_ncpus', type=int, metavar='INT', default=6, 
                          help=textwrap.dedent("""
                          Number of CPUs requested by Ray-Tune. Default: 6.
                          """ ).strip())
    
    optional.add_argument('--ray_ngpus', type=int, metavar='INT', default=1, 
                          help=textwrap.dedent("""
                          Number of GPUs requested by Ray-Tune. Default: 1.
                          """ ).strip())
    
    optional.add_argument('--cpu_per_trial', type=int, metavar='INT', default=3, 
                          help=textwrap.dedent("""
                          Number of CPUs used per trial. Default: 3.
                          """ ).strip())
    
    optional.add_argument('--gpu_per_trial', type=float, metavar='FLOAT', default=0.19, 
                          help=textwrap.dedent("""
                          Number of GPUs used per trial. Default: 0.19.
                          """ ).strip())
        
    optional.add_argument('--save_valid_preds', default=False, action='store_true', 
                          help=textwrap.dedent("""
                          Save prediction results for validation data in the checkpoint
                          folders. Default: False.
                          """ ).strip())
    
    optional.add_argument('--rerun_failed', default=False, action='store_true', 
                          help=textwrap.dedent("""
                          Rerun errored or incomplete trials. Default: False.
                          """ ).strip())

    parser._action_groups.append(optional)

    if len(sys.argv) == 1:
        parser.parse_args(['--help'])
    else:
        args = parser.parse_args()

    return args
def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
                                     description="""
    Overview
    --------
    
    This tool uses parameters from a pre-trained model to do train transfer learning models.
    The inputs include training and validation mutation data and the training results are saved
    under the "./ray_results/" folder.
    
    """)
    args = parse_arguments(parser)

    print(' '.join(sys.argv))
    
    train_file = args.train_data
    valid_file = args.validation_data
    ref_genome= args.ref_genome
    local_radius = args.local_radius
    local_order = args.local_order
    local_hidden1_size = args.local_hidden1_size
    local_hidden2_size = args.local_hidden2_size
    distal_radius = args.distal_radius  
    distal_order = args.distal_order
    emb_dropout = args.emb_dropout
    local_dropout = args.local_dropout
    batch_size = args.batch_size
    pred_batch_size = args.pred_batch_size
    CNN_kernel_size = args.CNN_kernel_size   
    CNN_out_channels = args.CNN_out_channels
    distal_fc_dropout = args.distal_fc_dropout
    n_class = args.n_class
    model_no = args.model_no
    seq_only = args.seq_only
    #pred_file = args.pred_file
    valid_ratio = args.valid_ratio
    save_valid_preds = args.save_valid_preds
    rerun_failed = args.rerun_failed

    
    ray_ncpus = args.ray_ncpus
    ray_ngpus = args.ray_ngpus
    cpu_per_trial = args.cpu_per_trial
    gpu_per_trial = args.gpu_per_trial
    cuda_id = args.cuda_id
    
    optim = args.optim
    learning_rate = args.learning_rate   
    weight_decay = args.weight_decay  
    LR_gamma = args.LR_gamma  
    epochs = args.epochs
    
    grace_period = args.grace_period
    n_trials = args.n_trials
    experiment_name = args.experiment_name
    ASHA_metric = args.ASHA_metric
    
    train_all = args.train_all
    init_fc_with_pretrained = args.init_fc_with_pretrained
    
    model_path = args.model_path
    #calibrator_path = args.calibrator_path
    model_config_path = args.model_config_path

    if args.split_seed < 0:
        args.split_seed = random.randint(0, 10000)
    print('args.split_seed:', args.split_seed)

    # Load model config (hyperparameters)
    if model_config_path != '':
        with open(model_config_path, 'rb') as fconfig:
            config = pickle.load(fconfig)
            
            local_radius = config['local_radius']
            local_order = config['local_order']
            local_hidden1_size = config['local_hidden1_size']
            local_hidden2_size = config['local_hidden2_size']
            distal_radius = config['distal_radius']
            distal_order = 1 # reserved for future improvement
            CNN_kernel_size = config['CNN_kernel_size']  
            CNN_out_channels = config['CNN_out_channels']
            emb_dropout = config['emb_dropout']
            local_dropout = config['local_dropout']
            distal_fc_dropout = config['distal_fc_dropout']
            emb_dims = config['emb_dims']

            args.n_class = config['n_class']
            args.model_no = config['model_no']
            
            args.seq_only = config['seq_only']
    
    
    start_time = time.time()
    print('Start time:', datetime.datetime.now())

    if ray_ngpus > 0 or gpu_per_trial > 0:
        if not torch.cuda.is_available():
            print('Error: You requested GPU computing, but CUDA is not available! If you want to run without GPU, please set "--ray_ngpus 0 --gpu_per_trial 0"', file=sys.stderr)
            sys.exit()
        # Set visible GPU(s)
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_id
        print('Ray is using GPU device', 'cuda:'+cuda_id)
    else:
        print('Ray is using only CPUs ...')
    
    if rerun_failed:
        resume_flag = 'ERRORED_ONLY'
    else:
        resume_flag = False
    
    # Allocate CPU/GPU resources for this Ray job
    ray.init(num_cpus=ray_ncpus, num_gpus=ray_ngpus, dashboard_host="0.0.0.0")
    
    # Prepare min/max for the loguniform samplers if one value is provided
    if len(learning_rate) == 1:
        learning_rate = learning_rate*2
    if len(weight_decay) == 1:
        weight_decay = weight_decay*2
    
    # Read bigWig file names
    bw_paths = args.bw_paths
    bw_files = []
    bw_names = []
    n_cont = 0
    
    if bw_paths:
        try:
            bw_list = pd.read_table(bw_paths, sep='\s+', header=None, comment='#')
            bw_files = list(bw_list[0])
            bw_names = list(bw_list[1])
            n_cont = len(bw_names)
        except pd.errors.EmptyDataError:
            print('Warnings: no bigWig files provided in', bw_paths)
    else:
        print('NOTE: no bigWig files provided.')
    # Read the train datapoints
    train_bed = BedTool(train_file)
    
    # Generate H5 files for storing distal regions before training, one file for each possible distal radius
    h5f_path = get_h5f_path(train_file, bw_names, distal_radius, distal_order)
    generate_h5f(train_bed, h5f_path, ref_genome, distal_radius, distal_order, bw_files, 1)
    
    if valid_file != '':
        valid_bed = BedTool(valid_file)
        valid_h5f_path = get_h5f_path(valid_file, bw_names, distal_radius, distal_order)
        generate_h5f(valid_bed, valid_h5f_path, ref_genome, distal_radius, distal_order, bw_files, 1)
    
    sys.stdout.flush()
    
    # Configure the search space for relavant hyperparameters
    config_ray = {
        'local_radius': local_radius,
        'local_order': local_order,
        'local_hidden1_size': local_hidden1_size,
        'local_hidden2_size': local_hidden2_size,
        'distal_radius': distal_radius,
        'emb_dropout': emb_dropout,
        'local_dropout': local_dropout,
        'CNN_kernel_size': CNN_kernel_size,
        'CNN_out_channels': CNN_out_channels,
        'distal_fc_dropout': distal_fc_dropout,
        'batch_size': batch_size,
        'learning_rate': tune.loguniform(learning_rate[0], learning_rate[1]),
        #'learning_rate': tune.choice(learning_rate),
        'optim': tune.choice(optim),
        'LR_gamma': tune.choice(LR_gamma),
        'weight_decay': tune.loguniform(weight_decay[0], weight_decay[1]),
        'transfer_learning': True,
        'train_all': train_all,
        'init_fc_with_pretrained': init_fc_with_pretrained,
        'emb_dims':emb_dims,
    }
    
    # Set the scheduler for parallel training 
    scheduler = ASHAScheduler(
    #metric='loss',
    metric=ASHA_metric, # Use a metric for model selection
    mode='min',
    max_t=epochs,
    grace_period=grace_period,
    reduction_factor=2)
    
    # Information to be shown in the progress table
    reporter = CLIReporter(parameter_columns=['local_radius', 'local_order', 'local_hidden1_size', 'local_hidden2_size', 'distal_radius', 'emb_dropout', 'local_dropout', 'CNN_kernel_size', 'CNN_out_channels', 'distal_fc_dropout', 'transfer_learning', 'train_all', 'init_fc_with_pretrained', 'optim', 'learning_rate', 'weight_decay', 'LR_gamma'], metric_columns=['loss', 'fdiri_loss', 'after_min_loss','score', 'total_params', 'training_iteration'])
    
    trainable_id = 'Train'
    tune.register_trainable(trainable_id, partial(train, args=args))
    
    # Execute the training
    result = tune.run(
    trainable_id,
    name=experiment_name,
    resources_per_trial={'cpu': cpu_per_trial, 'gpu': gpu_per_trial},
    config=config_ray,
    num_samples=n_trials,
    local_dir='./ray_results',
    scheduler=scheduler,
    stop={'after_min_loss':3},
    progress_reporter=reporter,
    resume=resume_flag)   
    
    
if __name__ == "__main__":
    main()


