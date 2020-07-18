from __future__ import print_function

import os
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
import h5py
import multiprocessing 
import re
import argparse
import json
import sys
import random
import numpy as np
import networkx as nx
import pandas as pd
import glob
import gc
import seaborn as sns
from scipy.sparse import identity
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from sklearn.metrics.pairwise import cosine_similarity, euclidean_distances

from keras.layers import Input, Layer, Dense, Embedding
from keras.models import Model
from keras import backend as K
from keras.callbacks import Callback, TerminateOnNaN, TensorBoard, ModelCheckpoint, CSVLogger, EarlyStopping

import tensorflow as tf
from tensorflow.python.framework import ops
from tensorflow.python.ops import math_ops, control_flow_ops
from tensorflow.python.training import optimizer

from heat.utils import hyperboloid_to_poincare_ball, load_data, load_embedding
from heat.utils import perform_walks, determine_positive_and_negative_samples
from heat.losses import  hyperbolic_softmax_loss, hyperbolic_sigmoid_loss, hyperbolic_hypersphere_loss
from heat.generators import TrainingDataGenerator
from heat.visualise import draw_graph, plot_degree_dist
from heat.callbacks import Checkpointer
from ge import *
K.set_floatx("float64")
K.set_epsilon(1e-15)
np.set_printoptions(suppress=True)

# TensorFlow wizardry
config = tf.ConfigProto()

# Don't pre-allocate memory; allocate as-needed
config.gpu_options.allow_growth = True
 
# Only allow a total of half the GPU memory to be allocated
config.gpu_options.per_process_gpu_memory_fraction = 0.5

config.log_device_placement=False
config.allow_soft_placement=True

# Create a session with the above options specified.
K.tensorflow_backend.set_session(tf.Session(config=config))

def gans_to_hyperboloid(x):
    t = K.sqrt(1. + K.sum(K.square(x), axis=-1, keepdims=True))
    return tf.concat([x, t], axis=-1)

def euclidean_dot(x, y):
    axes = len(x.shape) - 1, len(y.shape) - 1
    return K.batch_dot(x, y, axes=axes)

def minkowski_dot(x, y):
    axes = len(x.shape) - 1, len(y.shape) -1
    return K.batch_dot(x[...,:-1], y[...,:-1], axes=axes) - K.batch_dot(x[...,-1:], y[...,-1:], axes=axes)

def hyperboloid_initializer(shape, r_max=1e-3):

    def poincare_ball_to_hyperboloid(X, append_t=True):
        x = 2 * X
        t = 1. + K.sum(K.square(X), axis=-1, keepdims=True)
        if append_t:
            x = K.concatenate([x, t], axis=-1)
        return 1 / (1. - K.sum(K.square(X), axis=-1, keepdims=True)) * x

    def sphere_uniform_sample(shape, r_max):
        num_samples, dim = shape
        X = tf.random_normal(shape=shape, dtype=K.floatx())
        X_norm = K.sqrt(K.sum(K.square(X), axis=-1, keepdims=True))
        U = tf.random_uniform(shape=(num_samples, 1), dtype=K.floatx())
        return r_max * U ** (1./dim) * X / X_norm

    w = sphere_uniform_sample(shape, r_max=r_max)
    # w = tf.random_uniform(shape=shape, minval=-r_max, maxval=r_max, dtype=K.floatx())
    return poincare_ball_to_hyperboloid(w)

class HyperboloidEmbeddingLayer(Layer):
    
    def __init__(self, 
        num_nodes, 
        embedding_dim, 
        **kwargs):
        super(HyperboloidEmbeddingLayer, self).__init__(**kwargs)
        self.num_nodes = num_nodes
        self.embedding_dim = embedding_dim

    def build(self, input_shape):
        # Create a trainable weight variable for this layer.
        self.embedding = self.add_weight(name='embedding', 
          shape=(self.num_nodes, self.embedding_dim),
          initializer=hyperboloid_initializer,
          trainable=True)
        super(HyperboloidEmbeddingLayer, self).build(input_shape)

    def call(self, idx):

        embedding = tf.gather(self.embedding, idx)
        # embedding = tf.nn.embedding_lookup(self.embedding, idx)

        return embedding

    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], self.embedding_dim + 1)
    
    def get_config(self):
        base_config = super(HyperboloidEmbeddingLayer, self).get_config()
        base_config.update({"num_nodes": self.num_nodes, "embedding_dim": self.embedding_dim})
        return base_config

class ExponentialMappingOptimizer(optimizer.Optimizer):
    
    def __init__(self, 
        lr=0.1, 
        use_locking=False,
        name="ExponentialMappingOptimizer"):
        super(ExponentialMappingOptimizer, self).__init__(use_locking, name)
        self.lr = lr

    def _apply_dense(self, grad, var):
        spacial_grad = grad[:,:-1]
        t_grad = -1 * grad[:,-1:]
        
        ambient_grad = tf.concat([spacial_grad, t_grad], axis=-1)
        tangent_grad = self.project_onto_tangent_space(var, ambient_grad)
        
        exp_map = self.exponential_mapping(var, - self.lr * tangent_grad)
        
        return tf.assign(var, exp_map)
        
    def _apply_sparse(self, grad, var):
        indices = grad.indices
        values = grad.values

        p = tf.gather(var, indices, name="gather_apply_sparse")
        # p = tf.nn.embedding_lookup(var, indices)

        spacial_grad = values[:, :-1]
        t_grad = -values[:, -1:]

        ambient_grad = tf.concat([spacial_grad, t_grad], axis=-1, name="optimizer_concat")

        tangent_grad = self.project_onto_tangent_space(p, ambient_grad)
        exp_map = self.exponential_mapping(p, - self.lr * tangent_grad)

        return tf.scatter_update(ref=var, indices=indices, updates=exp_map, name="scatter_update")
    
    def project_onto_tangent_space(self, hyperboloid_point, minkowski_ambient):
        return minkowski_ambient + minkowski_dot(hyperboloid_point, minkowski_ambient) * hyperboloid_point
   
    def exponential_mapping( self, p, x ):

        def normalise_to_hyperboloid(x):
            return x / K.sqrt( -minkowski_dot(x, x) )

        norm_x = K.sqrt( K.maximum(np.float64(0.), minkowski_dot(x, x) ) ) 
        ####################################################
        exp_map_p = tf.cosh(norm_x) * p
        
        idx = tf.cast( tf.where(norm_x > K.cast(0., K.floatx()), )[:,0], tf.int64)
        non_zero_norm = tf.gather(norm_x, idx)
        z = tf.gather(x, idx) / non_zero_norm

        updates = tf.sinh(non_zero_norm) * z
        dense_shape = tf.cast( tf.shape(p), tf.int64)
        exp_map_x = tf.scatter_nd(indices=idx[:,None], updates=updates, shape=dense_shape)
        
        exp_map = exp_map_p + exp_map_x 
        #####################################################
        # z = x / K.maximum(norm_x, K.epsilon()) # unit norm 
        # exp_map = tf.cosh(norm_x) * p + tf.sinh(norm_x) * z
        #####################################################
        exp_map = normalise_to_hyperboloid(exp_map) # account for floating point imprecision

        return exp_map

def build_model(num_nodes, args):

    x = Input(shape=(1 + 1 + args.num_negative_samples, ), 
        name="model_input", 
        dtype=tf.int32)
    y = HyperboloidEmbeddingLayer(num_nodes, args.embedding_dim, name="embedding_layer")(x)
    model = Model(x, y)

    return model


def load_weights(model, args):

    previous_models = sorted(glob.iglob(
        os.path.join(args.embedding_path, "*.csv.gz")))
    if len(previous_models) > 0:
        model_file = previous_models[-1]
        initial_epoch = int(model_file.split("/")[-1].split("_")[0])
        print ("previous models found in directory -- loading from file {} and resuming from epoch {}".format(model_file, initial_epoch))
        embedding_df = load_embedding(model_file)
        embedding = embedding_df.reindex(sorted(embedding_df.index)).values
        model.layers[1].set_weights([embedding])
    else:
        print ("no previous model found in {}".format(args.embedding_path))
        initial_epoch = 0

    return model, initial_epoch

def parse_args():
    '''
    parse args from the command line
    '''
    parser = argparse.ArgumentParser(description="HEAT algorithm for feature learning on complex networks")

    parser.add_argument("--edgelist", dest="edgelist", type=str, default=None,#default="datasets/cora_ml/edgelist.tsv",
        help="edgelist to load.")
    parser.add_argument("--features", dest="features", type=str, default=None,#default="datasets/cora_ml/feats.csv",
        help="features to load.")
    parser.add_argument("--labels", dest="labels", type=str, default=None,#default="datasets/cora_ml/labels.csv",
        help="path to labels")

    parser.add_argument("--seed", dest="seed", type=int, default=0,
        help="Random seed (default is 0).")
    parser.add_argument("--lr", dest="lr", type=np.float64, default=1.,
        help="Learning rate (default is 1.).")

    parser.add_argument("-e", "--num_epochs", dest="num_epochs", type=int, default=5,
        help="The number of epochs to train for (default is 5).")
    parser.add_argument("-b", "--batch_size", dest="batch_size", type=int, default=50, 
        help="Batch size for training (default is 50).")
    parser.add_argument("--nneg", dest="num_negative_samples", type=int, default=20, 
        help="Number of negative samples for training (default is 10).")
    parser.add_argument("--context-size", dest="context_size", type=int, default=1,
        help="Context size for generating positive samples (default is 3).")
    parser.add_argument("--patience", dest="patience", type=int, default=10,
        help="The number of epochs of no improvement in loss before training is stopped. (Default is 10)")

    parser.add_argument("-d", "--dim", dest="embedding_dim", type=int,
        help="Dimension of embeddings for each layer (default is 2).", default=2)

    parser.add_argument("-p", dest="p", type=float, default=1.,
        help="node2vec return parameter (default is 1.).")
    parser.add_argument("-q", dest="q", type=float, default=1.,
        help="node2vec in-out parameter (default is 1.).")
    parser.add_argument('--num-walks', dest="num_walks", type=int, default=20, 
        help="Number of walks per source (default is 10).")
    parser.add_argument('--walk-length', dest="walk_length", type=int, default=30, 
        help="Length of random walk from source (default is 80).")

    parser.add_argument("--sigma", dest="sigma", type=np.float64, default=1.,
        help="Width of gaussian (default is 1).")

    parser.add_argument("--alpha", dest="alpha", type=float, default=0, 
        help="Probability of randomly jumping to a similar node when walking.")

    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", 
        help="Use this flag to set verbosity of training.")
    parser.add_argument('--workers', dest="workers", type=int, default=2, 
        help="Number of worker threads to generate training patterns (default is 2).")

    parser.add_argument("--walks", dest="walk_path", default=None, 
        help="path to save random walks.")

    parser.add_argument("--embedding", dest="embedding_path", default=None, 
        help="path to save embedings.")

    parser.add_argument('--directed', action="store_true", help='flag to train on directed graph')

    parser.add_argument('--use-generator', action="store_true", help='flag to train using a generator')

    parser.add_argument('--visualise', action="store_true", 
        help='flag to visualise embedding (embedding_dim must be 2)')

    parser.add_argument('--no-walks', action="store_true", 
        help='flag to only train on edgelist (no random walks)')

    parser.add_argument('--all-negs', action="store_true", 
        help='flag to only train using all nodes as negative samples')

    parser.add_argument("--time", dest="time_threshold", type=float, default=1, 
        help="Probability of randomly walk to past")

    args = parser.parse_args()
    return args

def configure_paths(args):
    '''
    build directories on local system for output of model after each epoch
    '''


    if not os.path.exists(args.embedding_path):
        os.makedirs(args.embedding_path)
        print ("making {}".format(args.embedding_path))
    print ("saving embedding to {}".format(args.embedding_path))
def plot(embeddings):
    X=[]
    Y=[]
    Label=[]
    for i in range(0,30):
        X.append(embeddings[i][0])
        Y.append(embeddings[i][1])
    canvas_height = 15
    canvas_width = 15
    dot_size = 1000
    text_size = 18
    legend_setting = False #“brief” / “full” / False


    sns.set(style="whitegrid")

    # set canvas height & width
    plt.figure(figsize=(canvas_width, canvas_height))


    color_paltette=[(0,34,255),(136,190,70),(189,43,18),(97,165,246),(223,186,36),(135,101,175),(227,227,227)]
    pts_colors=list(range(30))
    for i in range(30):
        if(i<9 or i>20):
            pts_colors[i]="color_1"
        if(i==9 or i==20):
            pts_colors[i]="color_2"
        if(i==10 or i==19):
            pts_colors[i]="color_3"
        if(i==11 or i==18):
            pts_colors[i]="color_4"
        if(i==12 or i==17):
            pts_colors[i]="color_5"
        if(i==13 or i==16):
            pts_colors[i]="color_6"
        if(i==14 or i==15):
            pts_colors[i]="color_7"

    for i in range(7):
        color_paltette[i] = (color_paltette[i][0] / 255, color_paltette[i][1] / 255, color_paltette[i][2] / 255)
        
        
    # reorganize dataset
    draw_dataset = {'x': X,
                    'y': Y, 
                    'label':list(range(1, 30 + 1)),
                    'ptsize': dot_size,
                    "cpaltette": color_paltette,
                    'colors':pts_colors}

    #draw scatterplot points
    ax = sns.scatterplot(x = "x",y = "y", alpha = 1,s = draw_dataset["ptsize"],hue="colors", palette=draw_dataset["cpaltette"], legend = legend_setting, data = draw_dataset)


    return ax
def main():
    args = parse_args()
    print ("Configured paths")
   # if os.path.exists(args.embedding_path):
   #     os._exit(0)
    graph = nx.read_weighted_edgelist(args.edgelist, delimiter=" ", nodetype=None,create_using=nx.Graph())
    graph_int = nx.read_weighted_edgelist(args.edgelist, delimiter=" ", nodetype=int,create_using=nx.Graph())
    model = Struc2Vec(graph.to_directed(), walk_length=10, num_walks=8,workers=8, verbose=40 )
    walks=model.return_walk_list()
    walks_int=[]
    for one_walk in walks:
        walks_int.append( [int(i) for i in one_walk])
        
    walks=walks_int
    graph=graph_int
    #print(walks)
    
    

    assert not (args.visualise and args.embedding_dim > 2), "Can only visualise two dimensions"
    assert args.embedding_path is not None, "you must specify a path to save embedding"


    
    configure_paths(args)

    
    # build model
    num_nodes = len(graph)
    
    model = build_model(num_nodes, args)
    model, initial_epoch = load_weights(model, args)
    optimizer = ExponentialMappingOptimizer(lr=args.lr)
    loss = hyperbolic_softmax_loss(sigma=args.sigma)
    model.compile(optimizer=optimizer, 
        loss=loss, 
        target_tensors=[tf.placeholder(dtype=tf.int32)])
    model.summary()

    callbacks = [
        TerminateOnNaN(),
        EarlyStopping(monitor="loss", 
            patience=args.patience, 
            verbose=True),
        Checkpointer(epoch=initial_epoch, 
            nodes=sorted(graph.nodes()), 
            embedding_directory=args.embedding_path)
    ]            

    positive_samples, negative_samples, probs = \
            determine_positive_and_negative_samples(graph,walks,args)

  
    if args.use_generator:
        print ("Training with data generator with {} worker threads".format(args.workers))
        training_generator = TrainingDataGenerator(positive_samples,  
                probs,
                model,
                args)

        model.fit_generator(training_generator, 
            workers=args.workers,
            max_queue_size=10, 
            use_multiprocessing=args.workers>0, 
            epochs=args.num_epochs, 
            steps_per_epoch=len(training_generator),
            initial_epoch=initial_epoch, 
            verbose=args.verbose,
            callbacks=callbacks
        )

    else:
        print ("Training without data generator")

        train_x = np.append(positive_samples, negative_samples, axis=-1)
        train_y = np.zeros([len(train_x), 1, 1], dtype=np.int32 )

        model.fit(train_x, train_y,
            shuffle=True,
            batch_size=args.batch_size, 
            epochs=args.num_epochs, 
            initial_epoch=initial_epoch, 
            verbose=args.verbose,
            callbacks=callbacks
        )

    print ("Training complete")

    embedding = model.get_weights()[0]
    embedding = hyperboloid_to_poincare_ball(embedding)
    print(embedding)
    ax=plot(embedding)
    theta = np.linspace(0, 2 * np.pi, 200)
    x = np.cos(theta)
    y = np.sin(theta)
    ax.plot(x, y, color="black", linewidth=2)
    ax.axis("equal")
    ax.figure.savefig("Hyper.pdf",bbox_inches='tight')
    

if __name__ == "__main__":
    main()