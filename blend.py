from __future__ import division, print_function
from collections import Counter
from datetime import datetime
from glob import glob
import os
import pprint

import click
import numpy as np
import pandas as pd
import theano
from sklearn.metrics import confusion_matrix, make_scorer
from sklearn.grid_search import GridSearchCV
import xgboost as xgb


from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler, MinMaxScaler

from nolearn.lasagne import NeuralNet
from lasagne.updates import *
from lasagne.layers import get_all_layers

from ordinal_classifier import OrdinalClassifier
import iterator, nn, util

from definitions import *
from boost import *
from layers import *
from nn import *

#theano.sandbox.cuda.use("cpu")

#MIN_LEARNING_RATE = 0.000001
#MAX_MOMENTUM = 0.9721356783919598
START_MOM = 0.9
STOP_MOM = 0.95
#INIT_LEARNING_RATE = 0.00002
START_LR = 0.0005
END_LR = START_LR * 0.001
L1 = 1e-4
L2 = 0.005
N_ITER = 100
PATIENCE = 20
POWER = 0.5
N_HIDDEN_1 = 32
N_HIDDEN_2 = 32
BATCH_SIZE = 128

SCHEDULE = {
    'start': START_LR,
    60: START_LR / 10.0,
    80: START_LR / 100.0,
    90: START_LR / 1000.0,
    N_ITER: 'stop'
}

RESAMPLE_WEIGHTS = [1.360, 14.37, 6.637, 40.23, 49.61]
#RESAMPLE_WEIGHTS = [1, 3, 2, 4, 5]
RESAMPLE_PROB = 0.2
SHUFFLE_PROB = 0.5

def get_objective(l1=L1, l2=L2):
    class RegularizedObjective(Objective):

        def get_loss(self, input=None, target=None, aggregation=None,
                     deterministic=False, **kwargs):

            l1_layer = get_all_layers(self.input_layer)[1]

            loss = super(RegularizedObjective, self).get_loss(
                input=input, target=target, aggregation=aggregation,
                deterministic=deterministic, **kwargs)
            if not deterministic:
                return loss \
                    + l1 * lasagne.regularization.regularize_layer_params(
                        l1_layer, lasagne.regularization.l1) \
                    + l2 * lasagne.regularization.regularize_network_params(
                        self.input_layer, lasagne.regularization.l2)
            else:
                return loss
    return RegularizedObjective

def epsilon_insensitive(y, t, d0=0.05, d1=0.5):
    #return T.maximum(epsilon**2.0, (y - t)**2.0) - epsilon ** 2.0
    #return T.maximum((abs(y - t) - epsilon)**2.0, 0.0)
    #return T.maximum(abs(y/eps - t/eps), (y/eps - t/eps)**2) * eps
    #return T.switch(T.lt(abs(y - t), eps), abs(y - t), (y - t)**2 / eps)
    #return T.switch(T.lt(abs(y - t), eps), 0.5 * (y - t)**2.0, 
    #                                       eps * (abs(y - t) - 0.5 * eps))
    a = abs(y - t)
    #huber = T.switch(T.lt(a, d1), (a - d0)**2.0, a - d1 + (d1 - d0)**2.0)
    #return T.switch(T.lt(a, d0), 0.0, huber)
    return T.switch(T.lt(a, d0), 0.0, (a - d0)**2.0)

def shuffle(*arrays):
    p = np.random.permutation(len(arrays[0]))
    return [array[p] for array in arrays]

class ResampleIterator(BatchIterator):
    def __iter__(self):
        n_samples = self.X.shape[0]
        bs = self.batch_size
        indices = util.balance_per_class_indices(self.y.ravel(), 
                                                 weights=RESAMPLE_WEIGHTS)
        for i in range((n_samples + bs - 1) // bs):
            r = np.random.rand()
            if r < RESAMPLE_PROB:
                sl = indices[np.random.randint(0, n_samples, size=bs)]
            elif r < SHUFFLE_PROB:
                sl = np.random.randint(0, n_samples, size=bs)
            else:
                sl = slice(i * bs, (i + 1) * bs)
            Xb = self.X[sl]
            if self.y is not None:
                yb = self.y[sl]
            else:
                yb = None
            yield self.transform(Xb, yb)

class ShufflingBatchIteratorMixin(object):
    def __iter__(self):
        if not hasattr(self, 'count'):
            self.count = 0
            self.interval = 1
        self.count += 1
        if self.count % self.interval == 0:
            print('shuffle')
            self.interval = self.count * 2
            self.X, self.y = shuffle(self.X, self.y)
        for X, y in super(ShufflingBatchIteratorMixin, self).__iter__():
            #X = X + np.random.randn(*X.shape).astype(np.float32) * 0.05
            yield X, y


class ShuffleIterator(ShufflingBatchIteratorMixin, BatchIterator):
    pass


class AdjustVariable(object):
    def __init__(self, name, start=0.03, stop=0.001):
        self.name = name
        self.start, self.stop = start, stop
        self.ls = None

    def __call__(self, nn, train_history):
        if self.ls is None:
            self.ls = np.linspace(self.start, self.stop, nn.max_epochs)

        epoch = train_history[-1]['epoch']
        new_value = float32(self.ls[epoch - 1])
        getattr(nn, self.name).set_value(new_value)


class AdjustPower(object):
    def __init__(self, name, start=START_LR, power=POWER):
        self.name = name
        self.start = start
        self.power = power
        self.ls = None

    def __call__(self, nn, train_history):
        if self.ls is None:
            self.ls = self.start * np.array(
                [(1.0 - float(n) / nn.max_epochs) ** self.power
                for n in range(nn.max_epochs)])

        epoch = train_history[-1]['epoch']
        new_value = float32(self.ls[epoch - 1])
        getattr(nn, self.name).set_value(new_value)


def get_estimator(n_features, **kwargs):
    l = [
        (InputLayer, {'shape': (None, n_features)}),
        #(DropoutLayer, {'p': 0.5}),
        (DenseLayer, {'num_units': N_HIDDEN_1, 'nonlinearity': leaky_rectify,
                      'W': init.Orthogonal('relu'), 'b':init.Constant(0.1)}),
        (FeaturePoolLayer, {'pool_size': 2}),
        #(DropoutLayer, {'p': 0.5}),
        #(FeatureWTALayer, {'pool_size': 2}),
        (DenseLayer, {'num_units': N_HIDDEN_2, 'nonlinearity': leaky_rectify,
                      'W': init.Orthogonal('relu'), 'b':init.Constant(0.1)}),
        (FeaturePoolLayer, {'pool_size': 2}),
        #(FeatureWTALayer, {'pool_size': 2}),
        #(DropoutLayer, {'p': 0.5}),
        #(DenseLayer, {'num_units': 128, 'nonlinearity': leaky_rectify,
        #              'W': init.Orthogonal('relu'), 'b':init.Constant(0.1)}),
        #(DropoutLayer, {'p': 0.5}),
        #(DenseLayer, {'num_units': 128, 'nonlinearity': leaky_rectify,
        #              'W': init.Orthogonal('relu'), 'b':init.Constant(0.1)}),
        #(DropoutLayer, {'p': 0.5}),
        #(DenseLayer, {'num_units': 128, 'nonlinearity': leaky_rectify}),
        (DenseLayer, {'num_units': 1, 'nonlinearity': None}),
    ]
    args = dict(
    
        #update=nesterov_momentum,
        update=adam,
        #update=rmsprop,
        #update=adadelta,
        update_learning_rate=theano.shared(float32(START_LR)),
        #update_momentum=theano.shared(float32(START_MOM)),

        #batch_iterator_train=ShuffleIterator(BATCH_SIZE),
        batch_iterator_train=ResampleIterator(BATCH_SIZE),

        objective=get_objective(),
        #objective_loss_function=epsilon_insensitive,

        eval_size=0.1,
        custom_score=('kappa', util.kappa) \
            if kwargs.get('eval_size', 0.1) > 0.0 else None,

        on_epoch_finished = [
            #AdjustPower('update_learning_rate', start=START_LR),
            Schedule('update_learning_rate', SCHEDULE),
            #AdjustVariable('update_momentum', start=START_MOM, stop=STOP_MOM),
            #AdjustPower('update_momentum', start=START_MOM, power=0.5),
            #AdjustLearningRate('update_learning_rate', loss='kappa', 
            #                   greater_is_better=True, patience=PATIENCE,
            #                   save=False),
        ],

        regression=True,
        max_epochs=N_ITER,
        verbose=1,
    )
    args.update(kwargs)
    return Net(l, **args)


@click.command()
@click.option('--cnf', default='config/c_512_4x4_very.py',
              help="Path or name of configuration module.")
@click.option('--predict', is_flag=True, default=False)
@click.option('--grid_search', is_flag=True, default=False)
@click.option('--per_patient', is_flag=True, default=False)
@click.option('--transform_file', default=None)
@click.option('--n_iter', default=1)
def fit(cnf, predict, grid_search, per_patient, transform_file, n_iter):

    model = util.load_module(cnf).model
    files = util.get_image_files(model.get('train_dir', TRAIN_DIR))
    names = util.get_names(files)
    labels = util.get_labels(names).astype(np.float32)[:, np.newaxis]

    dirs = glob('data/features/*')

    X_trains = [load_transform(directory=directory, 
                               transform_file=transform_file)
                for directory in dirs]
    scalers = [StandardScaler() for _ in X_trains]
    X_trains = [scaler.fit_transform(X_train) 
                for scaler, X_train in zip(scalers, X_trains)]
    #Xt = PCA(n_components=1).fit_transform(X_train)

    if per_patient:
        #X_train = per_patient_reshape(X_train, Xt).astype(np.float32)
        X_trains = [per_patient_reshape(X_train).astype(np.float32)
                    for X_train in X_trains]

    if predict:

        if transform_file is not None:
            transform_file = transform_file.replace('train', 'test')
        X_tests = [load_transform(directory=directory, test=True, 
                                  transform_file=transform_file)
                   for directory in dirs]

        X_tests = [scaler.transform(X_test) 
                   for scaler, X_test in zip(scalers, X_tests)]

        if per_patient:
            X_tests = [per_patient_reshape(X_test).astype(np.float32)
                       for X_test in X_tests]

    # util.split_indices split per patient by default now
    tr, te = util.split_indices(labels)

    # 
    if not predict:
        print("feature matrix {}".format(X_train.shape))

        if grid_search:
            kappa_scorer = make_scorer(util.kappa)
            gs = GridSearchCV(est, grid, verbose=3, cv=[(tr, te)], 
                              scoring=kappa_scorer, n_jobs=1, refit=False)
            gs.fit(X_train, labels)
            pd.set_option('display.height', 500)
            pd.set_option('display.max_rows', 500)
            df = util.grid_search_score_dataframe(gs)
            print(df)
            df.to_csv('grid_scores.csv')
            df.to_csv('grid_scores_{}.csv'.format(datetime.now().isoformat()))
            #est = gs.best_estimator_
        else:
            y_preds = []
            for i in range(n_iter):
                for X_train in X_trains:
                    print('iter {}'.format(i))
                    print('fitting split training set')
                    est = get_estimator(X_train.shape[1])
                    est.fit(X_train, labels)
                    y_pred = est.predict(X_train[te]).ravel()
                    y_preds.append(y_pred)
                    y_pred = np.mean(y_preds, axis=0)
                    y_pred  = np.clip(np.round(y_pred).astype(int),
                                      np.min(labels), np.max(labels))
                    print(labels[te].ravel().shape, y_pred.shape)
                    print('kappa', i, util.kappa(labels[te], y_pred))
                    print(confusion_matrix(labels[te], y_pred))

    if predict:

        y_preds = []
        for i in range(n_iter):
            for X_train, X_test in zip(X_trains, X_tests):
                print('fitting full training set')
                est = get_estimator(X_train.shape[1], eval_size=0.0)
                est.fit(X_train, labels)
                y_pred = est.predict(X_test).ravel()
                y_preds.append(y_pred)

        y_pred = np.mean(y_preds, axis=0)
        y_pred  = np.clip(np.round(y_pred),
                          np.min(labels), np.max(labels)).astype(int)

        submission_filename = util.get_submission_filename()
        files = util.get_image_files(model.get('test_dir', TEST_DIR))
        names = util.get_names(files)
        image_column = pd.Series(names, name='image')
        level_column = pd.Series(y_pred, name='level')
        predictions = pd.concat([image_column, level_column], axis=1)

        print(predictions.tail())

        predictions.to_csv(submission_filename, index=False)
        print("saved predictions to {}".format(submission_filename))


if __name__ == '__main__':
    fit()
