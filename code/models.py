"""
Copyright 2019 Twitter, Inc.
Licensed under the Apache License, Version 2.0
http://www.apache.org/licenses/LICENSE-2.0

Implementations of logistic LDA and baseline models for use with Tensorflow's Estimator.
"""

import numpy as np
import tensorflow as tf


def softmax_cross_entropy(targets, logits):
  """
  Implements a simple softmax cross entropy.

  $$-\sum_i t_{ni} \cdot (l_{ni} - \ln \sum_j \exp l_{nj})$$

  Targets can be arbitrary vectors and do not have to be one-hot encodings.

  Args:
    targets: A float tensor of shape [B, K]
    logits: A float tensor of shape [B, K]

  Returns:
    A float tensor of shape [B]
  """

  logprobs = logits - tf.reduce_logsumexp(logits, axis=1, keepdims=True)
  return -tf.reduce_sum(targets * logprobs, axis=1)


def create_table(keys, values=None):
  """
  Creates a hash table which maps the given keys to integers (by default).

  Args:
    keys: A list containing possible keys
    values: An optional list of corresponding values

  Returns:
    A `tf.contrib.lookup.HashTable` mapping keys to integers
  """

  if values is None:
    values = np.arange(len(keys), dtype=np.int64)

  return tf.contrib.lookup.HashTable(
    tf.contrib.lookup.KeyValueTensorInitializer(
      keys=keys,
      values=values), -1)


def mlp_fn(features, labels, mode, params):
  """
  Model function implementing a simple MLP which can be used for topic modeling.

  Args:
    features['embedding']: A tensor of shape [B, D]
    features['author_topic']: A tensor of shape [B] containing author labels as strings
    features['item_topic']: An tensor of shape [B] containing item labels (used in PREDICT only)
    labels: This will be ignored as labels are provided via `features`
    mode: Estimator's `ModeKeys`
    params['meta_info']['topics']: A list of strings of all possible topics
    params['hidden_units']: A list of integers describing the number of hidden units
    params['learning_rate']: Learning rate used with Adam
    params['decay_rate']: Exponential learning rate decay parameter
    params['decay_steps']: Exponential learning rate decay parameter
    params['embedding']: A function which preprocesses features

  Returns:
    A `tf.estimator.EstimatorSpec`
  """

  n_topics = len(params['meta_info']['topics'])

  # preprocess features (e.g., compute embeddings from words)
  features = params['embedding'](features)

  # convert string labels to integers
  topic_table = create_table(params['meta_info']['topics'])
  author_topics = topic_table.lookup(features['author_topic'])

  net = features['embedding']
  for units in params['hidden_units']:
    net = tf.layers.dense(
      net,
      units=units,
      activation=tf.nn.relu,
      kernel_regularizer=tf.contrib.layers.l2_regularizer(params['model_regularization']))

  logits = tf.layers.dense(
    net,
    n_topics,
    activation=None,
    kernel_regularizer=tf.contrib.layers.l2_regularizer(params['model_regularization']))

  if mode == tf.estimator.ModeKeys.PREDICT:
    probs = tf.reduce_max(tf.nn.softmax(logits), 1)
    predictions = tf.argmax(logits, 1)
    predictions = {
      'item_id': features['item_id'],
      'item_prediction': predictions,
      'item_probability': probs,
      'item_topic': topic_table.lookup(features['item_topic']),
      'author_id': features['author_id'],
      'author_prediction': predictions,
      'author_probability': probs,
      'author_topic': author_topics,
    }

    return tf.estimator.EstimatorSpec(mode, predictions=predictions)

  # model is trained to predict which topic an author belongs to
  loss = tf.reduce_mean(
    softmax_cross_entropy(
      targets=tf.one_hot(author_topics, depth=n_topics),
      logits=logits))

  tf.summary.scalar('loss', loss)

  if mode == tf.estimator.ModeKeys.EVAL:
    accuracy, acc_op = tf.metrics.accuracy(
      labels=author_topics,
      predictions=tf.argmax(logits, 1),
      name='acc_op')
    metric_ops = {'accuracy': (accuracy, acc_op)}
    return tf.estimator.EstimatorSpec(mode, loss=loss, eval_metric_ops=metric_ops)

  optimizer = tf.train.AdamOptimizer(
    learning_rate=tf.train.exponential_decay(
      learning_rate=params['learning_rate'],
      decay_rate=params['decay_rate'],
      decay_steps=params['decay_steps'],
      global_step=tf.train.get_global_step()))
  train_op = optimizer.minimize(loss, global_step=tf.train.get_global_step())

  return tf.estimator.EstimatorSpec(
    mode,
    loss=loss,
    train_op=train_op)


def logistic_lda_fn(features, labels, mode, params):
  """
  An implementation of logistic LDA.

  Args:
    features['embedding']: A tensor of shape [B, D]
    features['author_topic']: A tensor of shape [B] containing author labels as strings
    features['author_id']: A tensor of shape [B] containing integer IDs
    features['item_topic']: A tensor of shape [B] containing item labels (use '' if unknown)
    features['item_id']: A tensor of shape [B] containing integer IDs
    labels: This will be ignored as labels are provided via `features`
    mode: Estimator's `ModeKeys`
    params['meta_info']['topics']: A list of strings of all possible topics
    params['meta_info']['author_ids']: A list of all possible author IDs (these IDs group items)
    params['hidden_units']: A list of integers describing the number of hidden units
    params['learning_rate']: Learning rate used with Adam
    params['decay_rate']: Exponential learning rate decay parameter
    params['decay_steps']: Exponential learning rate decay parameter
    params['author_topic_weight']: Controls how much author topics influence the model
    params['author_topic_iterations']: Number of iterations to infer missing topics
    params['model_regularization']: Regularize model to make use of as many topics as possible
    params['alpha']: Smoothes topic distributions of authors
    params['embedding']: A function which preprocesses features
  """

  if params['author_topic_iterations'] < 1:
    raise ValueError('`author_topic_iterations` should be larger than 0.')

  n_authors = len(params['meta_info']['author_ids'])
  n_topics = len(params['meta_info']['topics'])

  # preprocess features (e.g., compute embeddings from words)
  features = params['embedding'](features)

  # lookup table which maps topics to indices and missing topics to -1
  topic_table = create_table(
    keys=params['meta_info']['topics'] + [''],
    values=list(range(n_topics)) + [-1])

  # convert string labels to integers
  author_topics = topic_table.lookup(features['author_topic'])
  item_topics = topic_table.lookup(features['item_topic'])

  # convert author IDs to low integers
  author_table = create_table(np.asarray(params['meta_info']['author_ids'], dtype=np.int64))
  author_ids = tf.squeeze(author_table.lookup(features['author_id']))

  # keeps track of topic counts per user
  topic_counts_var = tf.get_variable(
    'topic_counts',
    shape=[n_authors, n_topics],
    dtype=tf.float32,
    initializer=tf.ones_initializer,
    trainable=False)

  # keeps track of predicted topic distributions across all items
  topic_dist_total_var = tf.get_variable(
    'topic_dist_total',
    shape=[1, n_topics],
    initializer=tf.constant_initializer(1.0 / n_topics, dtype=tf.float32),
    trainable=False)

  # predict topics from items
  net = features['embedding']
  for units in params['hidden_units']:
    net = tf.layers.dense(net, units=units, activation=tf.nn.relu)

  # expected topic counts for each author
  topic_counts = tf.gather(topic_counts_var, author_ids)

  author_topics_onehot = tf.one_hot(tf.squeeze(author_topics), n_topics)
  author_topics_prediction = tf.ones_like(author_topics_onehot) / n_topics

  # infer missing author topics
  for _ in range(params['author_topic_iterations']):
    if params['use_author_topics']:
      # where available, use ground truth instead of predictions
      author_topics_prediction = tf.where(
        author_topics < 0,
        author_topics_prediction,
        author_topics_onehot)

    # update beliefs over author's topic distribution
    topic_biases = tf.digamma(
      params['alpha'] + topic_counts + params['author_topic_weight'] * author_topics_prediction)

    # update predictions of author topics
    author_topics_prediction = tf.nn.softmax(params['author_topic_weight'] * topic_biases)

  logits = tf.layers.dense(net, n_topics, activation=None)  # BxK
  logits_biased = logits + topic_biases

  # probability of each topic
  probs = tf.nn.softmax(logits)
  probs_biased = tf.nn.softmax(logits_biased)

  if mode == tf.estimator.ModeKeys.PREDICT:
    if params['author_topic_weight'] < 1e-8:
      author_topics_prediction = tf.nn.softmax(1e-8 * topic_biases)

    predictions = {
      'item_id': features.get('item_id', tf.zeros_like(author_ids) - 1),
      'item_prediction': tf.argmax(logits_biased, 1),
      'item_probability': tf.reduce_max(probs_biased, 1),
      'item_topic': item_topics,
      'author_id': author_ids,
      'author_prediction': tf.argmax(author_topics_prediction, 1),
      'author_probability': tf.reduce_max(author_topics_prediction, 1),
      'author_topic': author_topics,
    }
    return tf.estimator.EstimatorSpec(mode, predictions=predictions)

  # model is regularized to predict these topics
  expected_topics = (probs + 1e-6) / (topic_dist_total_var + 1e-6) / n_topics

  # the unbiased model tries to predict the biased topics
  loss = tf.reduce_mean(
    softmax_cross_entropy(
      targets=tf.stop_gradient(probs_biased + params['model_regularization'] * expected_topics),
      logits=logits))

  tf.summary.scalar('loss', loss)

  if mode == tf.estimator.ModeKeys.EVAL:
    # this assumes that all authors/items are labeled
    accuracy_author = tf.metrics.accuracy(
      labels=author_topics,
      predictions=tf.argmax(topic_counts, 1),
      name='acc_op')
    accuracy_item = tf.metrics.accuracy(
      labels=item_topics,
      predictions=tf.argmax(logits_biased, 1),
      name='acc_op')
    metric_ops = {
      'accuracy_author': accuracy_author,
      'accuracy_item': accuracy_item}

    return tf.estimator.EstimatorSpec(mode, loss=loss, eval_metric_ops=metric_ops)

  # update topic counters
  topic_counts_diff = probs_biased - topic_counts / params['max_items_per_author']
  topic_counts_update = tf.scatter_add(topic_counts_var, author_ids, topic_counts_diff)

  # update distribution of predicted topics
  topic_dist_diff = (probs - topic_dist_total_var) / (params['max_items_per_author'] * n_authors)
  topic_dist_total_update = tf.assign_add(
    topic_dist_total_var, tf.reduce_sum(topic_dist_diff, axis=0, keepdims=True))

  optimizer = tf.train.AdamOptimizer(
    learning_rate=tf.train.exponential_decay(
      learning_rate=params['learning_rate'],
      decay_rate=params['decay_rate'],
      decay_steps=params['decay_steps'],
      global_step=tf.train.get_global_step()))
  train_op = optimizer.minimize(loss, global_step=tf.train.get_global_step())

  # update model parameters, topic counts, and topic distribution estimate
  train_op = tf.group(train_op, topic_counts_update, topic_dist_total_update)

  return tf.estimator.EstimatorSpec(
    mode,
    loss=loss,
    train_op=train_op)
