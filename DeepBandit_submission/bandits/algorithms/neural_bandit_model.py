# Copyright 2018 The TensorFlow Authors All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Define a family of neural network architectures for bandits.

The network accepts different type of optimizers that could lead to different
approximations of the posterior distribution or simply to point estimates.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from absl import flags
from bandits.core.bayesian_nn import BayesianNN

FLAGS = flags.FLAGS


class NeuralBanditModel(BayesianNN):
  """Implements a neural network for bandit problems."""

  def __init__(self, optimizer, hparams, name):
    """Saves hyper-params and builds the Tensorflow graph."""

    self.opt_name = optimizer
    self.name = name
    self.hparams = hparams
    self.verbose = getattr(self.hparams, "verbose", True)
    self.times_trained = 0
    self.build_model()

  def build_layer(self, x, num_units):
    """Builds a layer with input x; dropout and layer norm if specified."""

    init_s = self.hparams.init_scale

    layer_n = getattr(self.hparams, "layer_norm", False)
    dropout = getattr(self.hparams, "use_dropout", False)

    nn = tf.contrib.layers.fully_connected(
        x,
        num_units,
        activation_fn=self.hparams.activation,
        normalizer_fn=None if not layer_n else tf.contrib.layers.layer_norm,
        normalizer_params={},
        weights_initializer=tf.random_uniform_initializer(-init_s, init_s)
    )

    if dropout:
      nn = tf.nn.dropout(nn, self.hparams.keep_prob)

    return nn

  def forward_pass(self):

    init_s = self.hparams.init_scale

    scope_name = "prediction_{}".format(self.name)
    with tf.variable_scope(scope_name, reuse=tf.AUTO_REUSE):
      nn = self.x
      for num_units in self.hparams.layer_sizes:
        if num_units > 0:
          nn = self.build_layer(nn, num_units)

      y_pred = tf.layers.dense(
          nn,
          self.hparams.num_actions,
          kernel_initializer=tf.random_uniform_initializer(-init_s, init_s))

    return nn, y_pred

  def build_model(self):
    """Defines the actual NN model with fully connected layers.

    The loss is computed for partial feedback settings (bandits), so only
    the observed outcome is backpropagated (see weighted loss).
    Selects the optimizer and, finally, it also initializes the graph.
    """

    # create and store the graph corresponding to the BNN instance
    self.graph = tf.Graph()

    with self.graph.as_default():

      # create and store a new session for the graph
      self.sess = tf.Session()

      with tf.name_scope(self.name):

        self.global_step = tf.train.get_or_create_global_step()

        # context
        self.x = tf.placeholder(
            shape=[None, self.hparams.context_dim],
            dtype=tf.float32,
            name="{}_x".format(self.name))

        # reward vector
        self.y = tf.placeholder(
            shape=[None, self.hparams.num_actions],
            dtype=tf.float32,
            name="{}_y".format(self.name))

        # weights (1 for selected action, 0 otherwise)
        self.weights = tf.placeholder(
            shape=[None, self.hparams.num_actions],
            dtype=tf.float32,
            name="{}_w".format(self.name))

        # with tf.variable_scope("prediction_{}".format(self.name)):
        self.nn, self.y_pred = self.forward_pass()
        self.loss = tf.squared_difference(self.y_pred, self.y)
        self.weighted_loss = tf.multiply(self.weights, self.loss)
        self.cost = tf.reduce_sum(self.weighted_loss) / self.hparams.batch_size

        if self.hparams.activate_decay:
          self.lr = tf.train.inverse_time_decay(
              self.hparams.initial_lr, self.global_step,
              1, self.hparams.lr_decay_rate)
        else:
          self.lr = tf.Variable(self.hparams.initial_lr, trainable=False)

        # create tensorboard metrics
        self.create_summaries()
        self.summary_writer = tf.summary.FileWriter(
            "{}/graph_{}".format(FLAGS.logdir, self.name), self.sess.graph)

        tvars = tf.trainable_variables()
        grads, _ = tf.clip_by_global_norm(
            tf.gradients(self.cost, tvars), self.hparams.max_grad_norm)

        self.optimizer = self.select_optimizer()

        self.train_op = self.optimizer.apply_gradients(
            zip(grads, tvars), global_step=self.global_step)

        self.init = tf.global_variables_initializer()

        self.initialize_graph()

  def initialize_graph(self):
    """Initializes all variables."""

    with self.graph.as_default():
      if self.verbose:
        print("Initializing model {}.".format(self.name))
      self.sess.run(self.init)

  def assign_lr(self):
    """Resets the learning rate in dynamic schedules for subsequent trainings.

    In bandits settings, we do expand our dataset over time. Then, we need to
    re-train the network with the new data. The algorithms that do not keep
    the step constant, can reset it at the start of each *training* process.
    """

    decay_steps = 1
    if self.hparams.activate_decay:
      current_gs = self.sess.run(self.global_step)
      with self.graph.as_default():
        self.lr = tf.train.inverse_time_decay(self.hparams.initial_lr,
                                              self.global_step - current_gs,
                                              decay_steps,
                                              self.hparams.lr_decay_rate)

  def select_optimizer(self):
    """Selects optimizer. To be extended (SGLD, KFAC, etc)."""
    return tf.train.RMSPropOptimizer(self.lr)

  def create_summaries(self):
    """Defines summaries including mean loss, learning rate, and global step."""

    with self.graph.as_default():
      with tf.name_scope(self.name + "_summaries"):
        tf.summary.scalar("cost", self.cost)
        tf.summary.scalar("lr", self.lr)
        tf.summary.scalar("global_step", self.global_step)
        self.summary_op = tf.summary.merge_all()

  def train(self, data, num_steps):
    """Trains the network for num_steps, using the provided data.

    Args:
      data: ContextualDataset object that provides the data.
      num_steps: Number of minibatches to train the network for.
    """

    if self.verbose:
      print("Training {} for {} steps...".format(self.name, num_steps))

    with self.graph.as_default():

      for step in range(num_steps):
        x, y, w = data.get_batch_with_weights(self.hparams.batch_size)
        _, cost, summary, lr = self.sess.run(
            [self.train_op, self.cost, self.summary_op, self.lr],
            feed_dict={self.x: x, self.y: y, self.weights: w})

        if step % self.hparams.freq_summary == 0:
          if self.hparams.show_training:
            print("{} | step: {}, lr: {}, loss: {}".format(
                self.name, step, lr, cost))
          self.summary_writer.add_summary(summary, step)

      self.times_trained += 1

  def get_mu_prior(self):
      with self.graph.as_default():
        with tf.name_scope(self.name):
            for v in tf.trainable_variables():
                if 'dense/kernel:0' in v.name:
                    weights = self.sess.run(v)
                if 'dense/bias:0' in v.name:
                    bias = self.sess.run(v)
      return weights,bias

  def set_last_layer(self,mu):
      #mu 40*7
      sec = self.hparams.layer_sizes[-1] #41
      weights = [[] for i in xrange(sec)] #7*40
      bias = []
      for mu_i in mu:
          bias.append(mu_i[-1])
          for j,w_j in enumerate(weights): #40
              w_j.append(mu_i[j])
      with self.graph.as_default():
        with tf.name_scope(self.name):
            for v in tf.trainable_variables():
                if 'dense/kernel:0' in v.name:
                    v.load(weights,self.sess)
                if 'dense/bias:0' in v.name:
                    v.load(bias,self.sess)


class TextCNN(object):
    """
    A CNN for text classification.
    Uses an embedding layer, followed by a convolutional, max-pooling and softmax layer.
    """

    def __init__(
      self,optimizer, num_classes,batch_size,name,sequence_length=60,vocab_size=18765,
      embedding_size=128, filter_sizes=[3,4,5], num_filters=128):

        # Placeholders for input, output and dropout

        self.lr = 1e-3
        self.name=name
        self.opt_name = optimizer
        self.num_actions = num_classes
        self.batch_size = batch_size
        # Keeping track of l2 regularization loss (optional)
        self.graph = tf.Graph()
        print("Initializing model {}.".format(self.name))

        with self.graph.as_default():
            self.sess = tf.Session()
            with tf.name_scope(self.name):
                # Embedding layer
                self.x = tf.placeholder(tf.int32, [None, sequence_length], name="input_x")
                self.y = tf.placeholder(tf.float32, [None,num_classes ], name="input_y")
                with tf.name_scope("embedding"):
                    self.W = tf.Variable(
                        tf.random_uniform([vocab_size, embedding_size], -1.0, 1.0),
                        name="W")
                    self.embedded_chars = tf.nn.embedding_lookup(self.W, self.x)
                    self.embedded_chars_expanded = tf.expand_dims(self.embedded_chars, -1)

                # Create a convolution + maxpool layer for each filter size
                pooled_outputs = []
                for i, filter_size in enumerate(filter_sizes):
                    with tf.name_scope("conv-maxpool-%s" % filter_size):
                        # Convolution Layer
                        filter_shape = [filter_size, embedding_size, 1, num_filters]
                        W = tf.Variable(tf.truncated_normal(filter_shape, stddev=0.1), name="W")
                        b = tf.Variable(tf.constant(0.1, shape=[num_filters]), name="b")
                        conv = tf.nn.conv2d(
                            self.embedded_chars_expanded,
                            W,
                            strides=[1, 1, 1, 1],
                            padding="VALID",
                            name="conv")
                        # Apply nonlinearity
                        h = tf.nn.relu(tf.nn.bias_add(conv, b), name="relu")
                        # Maxpooling over the outputs
                        pooled = tf.nn.max_pool(
                            h,
                            ksize=[1, sequence_length - filter_size + 1, 1, 1],
                            strides=[1, 1, 1, 1],
                            padding='VALID',
                            name="pool")
                        pooled_outputs.append(pooled)

                # Combine all the pooled features
                num_filters_total = num_filters * len(filter_sizes)
                self.h_pool = tf.concat(pooled_outputs, 3)
                self.last_h = tf.reshape(self.h_pool, [-1, num_filters_total])

                # weights (1 for selected action, 0 otherwise)
                self.weights = tf.placeholder(
                    shape=[None, self.num_actions],
                    dtype=tf.float32,
                    name="{}_w".format(self.name))

                # with tf.variable_scope("prediction_{}".format(self.name)):
                self.nn = tf.contrib.layers.fully_connected(
                    self.last_h,
                    50,
                    activation_fn=tf.nn.relu,
                    normalizer_fn= tf.contrib.layers.layer_norm,
                    normalizer_params={},
                    weights_initializer=tf.contrib.layers.xavier_initializer()
                )

                self.y_pred = tf.layers.dense(
                    self.nn,
                    self.num_actions,
                    kernel_initializer=tf.contrib.layers.xavier_initializer())

                self.loss = tf.squared_difference(self.y_pred, self.y)
                self.weighted_loss = tf.multiply(self.weights, self.loss)
                self.cost = tf.reduce_sum(self.weighted_loss) / self.batch_size

                tvars = tf.trainable_variables()
                grads = tf.gradients(self.cost, tvars)
                self.optimizer = self.select_optimizer()

                self.train_op = self.optimizer.apply_gradients(zip(grads, tvars))

                self.init = tf.global_variables_initializer()

                self.initialize_graph()

    def initialize_graph(self):
        """Initializes all variables."""
        with self.graph.as_default():
            self.sess.run(self.init)
    def select_optimizer(self):
        """Selects optimizer. To be extended (SGLD, KFAC, etc)."""
        return tf.train.AdamOptimizer(1e-3)
    def train(self, data, num_steps):
        """Trains the network for num_steps, using the provided data.

        Args:
          data: ContextualDataset object that provides the data.
          num_steps: Number of minibatches to train the network for.
        """
        print("Training {} for {} steps...".format(self.name, num_steps))

        with self.graph.as_default():

            for step in range(num_steps):
                x, y, w = data.get_batch_with_weights(self.batch_size)
                #x = [map(int,i) for i in x]
                #y = [map(int,i) for i in y]
                #w = [map(int,i) for i in w]
                self.sess.run([self.train_op, self.cost],feed_dict={self.x: x, self.y: y, self.weights: w})

    def assign_lr(self):

        decay_steps = 1

    def get_mu_prior(self):
        #h_pool_flat


      with self.graph.as_default():
        with tf.name_scope(self.name):
            for v in tf.trainable_variables():
                if 'dense/kernel:0' in v.name:
                    weights = self.sess.run(v)
                if 'dense/bias:0' in v.name:
                    bias = self.sess.run(v)
      return weights,bias

    def set_last_layer(self,mu):
      #mu 40*7
      sec = self.hparams.layer_sizes[-1] #41
      weights = [[] for i in xrange(sec)] #7*40
      bias = []
      for mu_i in mu:
          bias.append(mu_i[-1])
          for j,w_j in enumerate(weights): #40
              w_j.append(mu_i[j])
      with self.graph.as_default():
        with tf.name_scope(self.name):
            for v in tf.trainable_variables():
                if 'dense/kernel:0' in v.name:
                    v.load(weights,self.sess)
                if 'dense/bias:0' in v.name:
                    v.load(bias,self.sess)