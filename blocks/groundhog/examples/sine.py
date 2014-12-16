from __future__ import print_function

import argparse
import logging
import inspect
import pprint

import numpy
import theano
try:
    from groundhog.mainLoop import MainLoop
    from groundhog.trainer.SGD import SGD
    from matplotlib import pyplot
except:
    pass  # TODO matplotlib as dependency?
from theano import tensor

from blocks.bricks import Brick, Identity, Tanh, MLP, lazy, application
from blocks.bricks.parallel import Fork
from blocks.bricks.recurrent import GatedRecurrent
from blocks.select import Selector
from blocks.graph import apply_noise, ComputationGraph
from blocks.bricks.sequence_generators import (
    SequenceGenerator, LinearReadout, TrivialEmitter)
from blocks.initialization import Orthogonal, IsotropicGaussian, Constant
from blocks.groundhog import GroundhogIterator, GroundhogState, GroundhogModel
from blocks.serialization import load_params
from blocks.utils import update_instance

floatX = theano.config.floatX
logger = logging.getLogger()


class AddParameters(Brick):
    """Adds dependency on parameters to a transition function.

    In fact an improved version of this brick should be moved
    to the main body of the library, because it is clearly reusable
    (e.g. it can be a part of Encoder-Decoder translation model.

    """
    @lazy
    def __init__(self, transition, num_params, params_name,
                 weights_init, biases_init, **kwargs):
        super(AddParameters, self).__init__(**kwargs)
        update_instance(self, locals())

        self.input_names = [name for name in transition.apply.sequences
                            if name != 'mask']
        self.state_name = transition.apply.states[0]
        assert len(transition.apply.states) == 1

        self.fork = Fork(self.input_names)
        # Could be also several init bricks, one for each of the states
        self.init = MLP([Identity()], name="init")
        self.children = [self.transition, self.fork, self.init]

    def _push_allocation_config(self):
        self.fork.input_dim = self.num_params
        self.fork.fork_dims = {name: self.transition.get_dim(name)
                               for name in self.input_names}
        self.init.dims[0] = self.num_params
        self.init.dims[-1] = self.transition.get_dim(self.state_name)

    def _push_initialization_config(self):
        for child in self.children:
            if self.weights_init:
                child.weights_init = self.weights_init
            if self.biases_init:
                child.biases_init = self.biases_init

    @application
    def apply(self, **kwargs):
        inputs = {name: kwargs.pop(name) for name in self.input_names}
        params = kwargs.pop("params")
        forks = self.fork.apply(params, return_dict=True)
        for name in self.input_names:
            inputs[name] = inputs[name] + forks[name]
        kwargs.update(inputs)
        if kwargs.get('iterate', True):
            kwargs[self.state_name] = self.initial_state(None, params=params)
        return self.transition.apply(**kwargs)

    @apply.delegate
    def apply_delegate(self):
        return self.transition.apply

    @apply.property('contexts')
    def apply_contexts(self):
        return [self.params_name] + self.transition.apply.contexts

    @application
    def initial_state(self, batch_size, *args, **kwargs):
        return self.init.apply(kwargs['params'])

    def get_dim(self, name):
        if name == 'params':
            return self.num_params
        return self.transition.get_dim(name)


class SeriesIterator(GroundhogIterator):
    """Training data generator."""

    def __init__(self, rng, func, seq_len, batch_size):
        update_instance(self, locals())
        self.num_params = len(inspect.getargspec(self.func).args) - 1

    def next(self):
        """Generate random sequences from the family."""
        params = self.rng.uniform(size=(self.batch_size, self.num_params))
        params = params.astype(floatX)
        x = numpy.zeros((self.seq_len, self.batch_size, 1), dtype=floatX)
        for i in range(self.seq_len):
            x[i, :, 0] = self.func(*([list(params.T)] +
                                     [i * numpy.ones(self.batch_size)]))

        return dict(x=x, params=params)


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s: %(name)s: %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        "Case study of generating simple 1d sequences with RNN.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "mode", choices=["train", "plot"],
        help="The mode to run. Use `train` to train a new model"
             " and `plot` to plot a sequence generated by an"
             " existing one.")
    parser.add_argument(
        "prefix", default="sine",
        help="The prefix for model, timing and state files")
    parser.add_argument(
        "--input-noise", type=float, default=0.0,
        help="Adds Gaussian noise of given intensity to the "
             " training sequences.")
    parser.add_argument(
        "--function", default="lambda a, x: numpy.sin(a * x)",
        help="An analytical description of the sequence family to learn."
             " The arguments before the last one are considered parameters.")
    parser.add_argument(
        "--steps", type=int, default=100,
        help="Number of steps to plot")
    parser.add_argument(
        "--params",
        help="Parameter values for plotting")
    args = parser.parse_args()

    function = eval(args.function)
    num_params = len(inspect.getargspec(function).args) - 1

    class Emitter(TrivialEmitter):
        @application
        def cost(self, readouts, outputs):
            """Compute MSE."""
            return ((readouts - outputs) ** 2).sum(axis=readouts.ndim - 1)

    transition = GatedRecurrent(
        name="transition", activation=Tanh(), dim=10,
        weights_init=Orthogonal())
    with_params = AddParameters(transition, num_params, "params",
                                name="with_params")
    generator = SequenceGenerator(
        LinearReadout(readout_dim=1, source_names=["states"],
                      emitter=Emitter(name="emitter"), name="readout"),
        with_params,
        weights_init=IsotropicGaussian(0.01), biases_init=Constant(0),
        name="generator")
    generator.allocate()
    logger.debug("Parameters:\n" +
                 pprint.pformat(
                     [(key, value.get_value().shape) for key, value
                      in Selector(generator).get_params().items()],
                     width=120))

    if args.mode == "train":
        seed = 1
        rng = numpy.random.RandomState(seed)
        batch_size = 10

        generator.initialize()

        cost = ComputationGraph(
            generator.cost(tensor.tensor3('x'),
                           params=tensor.matrix("params")).sum())
        cost = apply_noise(cost, cost.inputs, args.input_noise)

        gh_model = GroundhogModel(generator, cost)
        state = GroundhogState(args.prefix, batch_size,
                               learning_rate=0.0001).as_dict()
        data = SeriesIterator(rng, function, 100, batch_size)
        trainer = SGD(gh_model, state, data)
        main_loop = MainLoop(data, None, None, gh_model, trainer, state, None)
        main_loop.load()
        main_loop.main()
    elif args.mode == "plot":
        load_params(generator,  args.prefix + "model.npz")

        params = tensor.matrix("params")
        sample = theano.function([params], generator.generate(
            params=params, n_steps=args.steps, batch_size=1))

        param_values = numpy.array(map(float, args.params.split()),
                                   dtype=floatX)
        states, outputs, _ = sample(param_values[None, :])
        actual = outputs[:, 0, 0]
        desired = numpy.array([function(*(list(param_values) + [T]))
                               for T in range(args.steps)])
        print("MSE: {}".format(((actual - desired) ** 2).sum()))

        pyplot.plot(numpy.hstack([actual[:, None], desired[:, None]]))
        pyplot.show()
    else:
        assert False


if __name__ == "__main__":
    main()
