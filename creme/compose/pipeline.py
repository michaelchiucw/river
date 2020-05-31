import collections
import functools
import io
import itertools
import types
import typing
import warnings

import pandas as pd

from .. import base

from . import func
from . import union


__all__ = ['Pipeline']


class Pipeline(base.Estimator):
    """A pipeline of estimators.

    Pipelines provide to organize different processing steps into a sequence of steps. Typically,
    when doing supervised learning, a pipeline contains one ore more transformation steps, whilst
    it's is a regressor or a classifier.

    It is highly recommended to use pipelines with `creme`. Indeed, in an online learning setting,
    it is very practical to have a model defined as a single object. On the contrary, in batch
    learning, this isn't as important.

    Take a look at the[user guide](/user-guide/the-art-of-using-pipeline) for further information
    and practical examples.

    Parameters:
        steps: Ideally a list of (name, estimator) tuples. If an estimator is given without a name,
            then a name is automatically inferred from the estimator.

    Attributes:
        steps (collections.OrderedDict)

    Example:

        >>> from creme import compose
        >>> from creme import linear_model
        >>> from creme import preprocessing

        The recommended way to declare a pipeline is to use the `|` operator. The latter allows you
        to chain estimators in a very terse manner:

        >>> from creme import linear_model
        >>> from creme import preprocessing

        >>> scaler = preprocessing.StandardScaler()
        >>> log_reg = linear_model.LinearRegression()
        >>> model = scaler | log_reg

        This results in a pipeline that stores each step inside a dictionary.

        >>> model
        Pipeline (
          StandardScaler (),
          LinearRegression (
            optimizer=SGD (
              lr=Constant (
                learning_rate=0.01
              )
            )
            loss=Squared ()
            l2=0.
            intercept=0.
            intercept_lr=Constant (
            learning_rate=0.01
            )
            clip_gradient=1e+12
            initializer=Zeros ()
          )
        )

        You can access parts of a pipeline in the same manner as a dictionary:

        >>> model['LinearRegression']
        LinearRegression (
          optimizer=SGD (
            lr=Constant (
              learning_rate=0.01
            )
          )
          loss=Squared ()
          l2=0.
          intercept=0.
          intercept_lr=Constant (
            learning_rate=0.01
          )
          clip_gradient=1e+12
          initializer=Zeros ()
        )

        Note that you can also declare a pipeline by using the `compose.Pipeline` constructor
        method, which is slighly more verbose:

        >>> from creme import compose

        >>> model = compose.Pipeline(scaler, log_reg)

        By using a `compose.TransformerUnion`, you can define complex pipelines that apply
        different steps to different parts of the data. For instance, we can extract word counts
        from text data, and extract polynomial features from numeric data.

        >>> from creme import feature_extraction

        >>> tfidf = feature_extraction.TFIDF('text')
        >>> counts = feature_extraction.BagOfWords('text')
        >>> text_part = compose.Select('text') | (tfidf + counts)

        >>> num_part = compose.Select('a', 'b') | preprocessing.PolynomialExtender()

        >>> model = text_part + num_part
        >>> model |= preprocessing.StandardScaler()
        >>> model |= linear_model.LinearRegression()

        You can obtain a visual representation of the pipeline by calling it's `draw` method.

        >>> dot = model.draw()

        ![pipeline_example](/img/pipeline_docstring.svg)

        The following shows an example of using `debug_one` to visualize how the information
        flows and changes throughout the pipeline.

        >>> from creme import compose
        >>> from creme import feature_extraction
        >>> from creme import naive_bayes

        >>> X_y = [
        ...     ('A positive comment', True),
        ...     ('A negative comment', False),
        ...     ('A happy comment', True),
        ...     ('A lovely comment', True),
        ...     ('A harsh comment', False)
        ... ]

        >>> tfidf = feature_extraction.TFIDF() | compose.Renamer(prefix='tfidf_')
        >>> counts = feature_extraction.BagOfWords() | compose.Renamer(prefix='count_')
        >>> mnb = naive_bayes.MultinomialNB()
        >>> model = (tfidf + counts) | mnb

        >>> for x, y in X_y:
        ...     model = model.fit_one(x, y)

        >>> x = X_y[0][0]
        >>> report = model.debug_one(X_y[0][0])
        >>> print(report)
        0. Input
        --------
        A positive comment
        <BLANKLINE>
        1. Transformer union
        --------------------
            1.0 TFIDF | Renamer
            -------------------
            tfidf_comment: 0.47606 (float)
            tfidf_positive: 0.87942 (float)
        <BLANKLINE>
            1.1 BagOfWords | Renamer
            ------------------------
            count_comment: 1 (int)
            count_positive: 1 (int)
        <BLANKLINE>
        count_comment: 1 (int)
        count_positive: 1 (int)
        tfidf_comment: 0.50854 (float)
        tfidf_positive: 0.86104 (float)
        <BLANKLINE>
        2. MultinomialNB
        ----------------
        False: 0.19313
        True: 0.80687

    """

    def __init__(self, *steps):
        self.steps = collections.OrderedDict()
        for step in steps:
            self |= step

    def __getitem__(self, key):
        """Just for convenience."""
        return self.steps[key]

    def __len__(self):
        """Just for convenience."""
        return len(self.steps)

    def __or__(self, other):
        """Inserts a step at the end of the pipeline."""
        self._add_step(other, at_start=False)
        return self

    def __ror__(self, other):
        """Inserts a step at the start of the pipeline."""
        self._add_step(other, at_start=True)
        return self

    def __add__(self, other):
        """Merges with another Pipeline or TransformerUnion into a TransformerUnion."""
        if isinstance(other, union.TransformerUnion):
            return other.__add__(self)
        return union.TransformerUnion(self, other)

    def __str__(self):
        return ' | '.join(map(str, self.steps.values()))

    def __repr__(self):
        return (
            'Pipeline (\n\t' +
            '\t'.join(',\n'.join(map(repr, self.steps.values())).splitlines(True)) +
            '\n)'
        ).expandtabs(2)

    def _get_params(self):
        return dict(self.steps.items())

    def _set_params(self, new_params=None):
        if new_params is None:
            new_params = {}
        return Pipeline(*[
            (name, new_params[name])
            if isinstance(new_params.get(name), base.Estimator) else
            (name, step._set_params(new_params.get(name, {})))
            for name, step in self.steps.items()
        ])

    def _add_step(self, estimator, at_start: bool):
        """Adds a step to either end of the pipeline.

        This method takes care of sanitizing the input. For instance, if a function is passed,
        then it will be wrapped with a `FuncTransformer`.

        """

        name = None
        if isinstance(estimator, tuple):
            name, estimator = estimator

        # If the step is a function then wrap it in a FuncTransformer
        if isinstance(estimator, (types.FunctionType, types.LambdaType)):
            estimator = func.FuncTransformer(estimator)

        def infer_name(estimator):
            if isinstance(estimator, func.FuncTransformer):
                return infer_name(estimator.func)
            elif isinstance(estimator, (types.FunctionType, types.LambdaType)):
                return estimator.__name__
            elif hasattr(estimator, '__class__'):
                return estimator.__class__.__name__
            return str(estimator)

        # Infer a name if none is given
        if name is None:
            name = infer_name(estimator)

        if name in self.steps:
            counter = 1
            while f'{name}{counter}' in self.steps:
                counter += 1
            name = f'{name}{counter}'

        # Instantiate the estimator if it hasn't been done
        if isinstance(estimator, type):
            estimator = estimator()

        # Store the step
        self.steps[name] = estimator

        # Move the step to the start of the pipeline if so instructed
        if at_start:
            self.steps.move_to_end(name, last=False)

    # Single instance methods

    def fit_one(self, x: dict, y=None, **params):
        """Fit to a single instance.

        Parameters:
            X: A dictionary of features.
            y: A target value.

        """

        steps = iter(self.steps.values())

        # Loop over the first n - 1 steps, which should all be transformers
        for t in itertools.islice(steps, len(self.steps) - 1):
            x_pre = x
            x = t.transform_one(x=x)

            # The supervised transformers have to be updated.
            # Note that this is done after transforming in order to avoid target leakage.
            if isinstance(t, union.TransformerUnion):
                for sub_t in t.transformers.values():
                    if isinstance(sub_t, base.SupervisedTransformer):
                        sub_t.fit_one(x=x_pre, y=y)

            elif isinstance(t, base.SupervisedTransformer):
                t.fit_one(x=x_pre, y=y)

        # At this point steps contains a single step, which is therefore the final step of the
        # pipeline
        final = next(steps)
        if final._is_supervised:
            final.fit_one(x=x, y=y, **params)
        else:
            final.fit_one(x=x, **params)

        return self

    def _transform_one(self, x: dict):
        """This methods takes care of applying the first n - 1 steps of the pipeline, which are
        supposedly transformers. It also returns the final step so that other functions can do
        something with it.

        """

        steps = iter(self.steps.values())

        for t in itertools.islice(steps, len(self.steps) - 1):

            # The unsupervised transformers are updated during transform. We do this because
            # typically transform_one is called before fit_one, and therefore we might as well use
            # the available information as soon as possible. Note that way of proceeding is very
            # specific to online machine learning.
            if isinstance(t, union.TransformerUnion):
                for sub_t in t.transformers.values():
                    if not isinstance(t, base.SupervisedTransformer):
                        sub_t.fit_one(x=x)

            elif not isinstance(t, base.SupervisedTransformer):
                t.fit_one(x=x)

            x = t.transform_one(x=x)

        return x, next(steps)

    def transform_one(self, x: dict):
        """Apply each transformer in the pipeline to some features.

        The final step in the pipeline will be applied if it is a transformer. If not, then it will
        be ignored and the output from the penultimate step will be returned. Note that the steps
        that precede the final step are assumed to all be transformers.

        """
        x, final_step = self._transform_one(x=x)
        if isinstance(final_step, base.Transformer):
            return final_step.transform_one(x=x)
        return x

    def predict_one(self, x: dict):
        x, final_step = self._transform_one(x=x)
        return final_step.predict_one(x=x)

    def predict_proba_one(self, x: dict):
        x, final_step = self._transform_one(x=x)
        return final_step.predict_proba_one(x=x)

    def score_one(self, x: dict):
        x, final_step = self._transform_one(x=x)
        return final_step.score_one(x=x)

    def forecast(self, horizon: int, xs: typing.List[dict] = None):
        """Return a forecast.

        Only works if each estimator has a `transform_one` method and the final estimator has a
        `forecast` method. This is the case of time series models from the `time_series` module.

        Parameters:
            horizon: The forecast horizon.
            xs: A list of features for each step in the horizon.

        """
        if xs is not None:
            xs = [self._transform_one(x)[0] for x in xs]
        final_step = list(self.steps.values())[-1]
        return final_step.forecast(horizon=horizon, xs=xs)

    def debug_one(self, x: dict, show_types=True, n_decimals=5) -> str:
        """Displays the state of a set of features as it goes through the pipeline.

        Parameters:
            x A set of features.
            show_types: Whether or not to display the type of feature along with it's value.
            n_decimals: Number of decimals to display for each floating point value.

        """

        tab = ' ' * 4

        # We'll redirect all the print statement to a buffer, we'll return the content of the
        # buffer at the end
        buffer = io.StringIO()
        _print = functools.partial(print, file=buffer)

        def format_value(x):
            if isinstance(x, float):
                return '{:,.{prec}f}'.format(x, prec=n_decimals)
            return x

        def print_dict(x, show_types, indent=False, space_after=True):

            # Some transformers accept strings as input instead of dicts
            if isinstance(x, str):
                _print(x)
            else:
                for k, v in sorted(x.items()):
                    type_str = f' ({type(v).__name__})' if show_types else ''
                    _print((tab if indent else '') + f'{k}: {format_value(v)}' + type_str)
            if space_after:
                _print()

        def print_title(title, indent=False):
            _print((tab if indent else '') + title)
            _print((tab if indent else '') + '-' * len(title))

        # Print the initial state of the features
        print_title('0. Input')
        print_dict(x, show_types=show_types)

        # Print the state of x at each step
        steps = iter(self.steps.values())
        for i, t in enumerate(itertools.islice(steps, len(self.steps) - 1)):

            if isinstance(t, union.TransformerUnion):
                print_title(f'{i+1}. Transformer union')
                for j, (name, sub_t) in enumerate(t.transformers.items()):
                    if isinstance(sub_t, Pipeline):
                        name = str(sub_t)
                    print_title(f'{i+1}.{j} {name}', indent=True)
                    print_dict(sub_t.transform_one(x), show_types=show_types, indent=True)
                x = t.transform_one(x)
                print_dict(x, show_types=show_types)

            else:
                print_title(f'{i+1}. {t}')
                x = t.transform_one(x)
                print_dict(x, show_types=show_types)

        # Print the predicted output from the final estimator
        final = next(steps)
        if not isinstance(final, base.Transformer):
            print_title(f'{len(self)}. {final}')

            # If the last estimator has a debug_one method then call it
            if hasattr(final, 'debug_one'):
                _print(final.debug_one(x))

            # Display the prediction
            _print()
            if isinstance(final, base.Classifier):
                print_dict(final.predict_proba_one(x), show_types=False, space_after=False)
            else:
                _print(f'Prediction: {format_value(final.predict_one(x))}')

        return buffer.getvalue().rstrip()

    # Mini-batch methods

    def fit_many(self, X: pd.DataFrame, y: pd.Series, **params):
        """Fit to a mini-batch.

        Parameters:
            X: A dataframe of features. Columns can be added and/or removed between successive
                calls.
            y: A series of target values.

        """

        steps = iter(self.steps.values())

        # Loop over the first n - 1 steps, which should all be transformers
        for t in itertools.islice(steps, len(self.steps) - 1):
            X_pre = X
            X = t.transform_many(X=X)

            # The supervised transformers have to be updated.
            # Note that this is done after transforming in order to avoid target leakage.
            if isinstance(t, union.TransformerUnion):
                for sub_t in t.transformers.values():
                    if isinstance(sub_t, base.SupervisedTransformer):
                        sub_t.fit_many(X=X)

            elif isinstance(t, base.SupervisedTransformer):
                t.fit_many(X=X)

        # At this point steps contains a single step, which is therefore the final step of the
        # pipeline
        final = next(steps)

        if final._is_supervised:
            final.fit_many(X=X, y=y, **params)
        else:
            final.fit_many(X=X, y=y, **params)

        return self

    def _transform_many(self, X: pd.DataFrame):
        """This methods takes care of applying the first n - 1 steps of the pipeline, which are
        supposedly transformers. It also returns the final step so that other functions can do
        something with it.

        """

        steps = iter(self.steps.values())

        for t in itertools.islice(steps, len(self.steps) - 1):

            # The unsupervised transformers are updated during transform. We do this because
            # typically transform_one is called before fit_one, and therefore we might as well use
            # the available information as soon as possible. Note that way of proceeding is very
            # specific to online machine learning.
            if isinstance(t, union.TransformerUnion):
                for sub_t in t.transformers.values():
                    if not isinstance(t, base.SupervisedTransformer):
                        sub_t.fit_many(X=X)

            elif not isinstance(t, base.SupervisedTransformer):
                t.fit_many(X=X)

            X = t.transform_many(X=X)

        return X, next(steps)

    def transform_many(self, X: pd.DataFrame):
        """Apply each transformer in the pipeline to some features.

        The final step in the pipeline will be applied if it is a transformer. If not, then it will
        be ignored and the output from the penultimate step will be returned. Note that the steps
        that precede the final step are assumed to all be transformers.

        """
        X, final_step = self._transform_many(X=X)
        if isinstance(final_step, base.Transformer):
            return final_step.transform_many(X=X)
        return X

    def predict_many(self, X: pd.DataFrame):
        X, final_step = self._transform_many(X=X)
        return final_step.predict_many(X=X)

    def predict_proba_many(self, X: pd.DataFrame):
        X, final_step = self._transform_many(X=X)
        return final_step.predict_proba_many(X=X)

    def draw(self):
        """Draws the pipeline using the `graphviz` library."""

        def networkify(step):

            # Unions are converted to an undirected network
            if isinstance(step, union.TransformerUnion):
                return Network(
                    nodes=map(networkify, step.transformers.values()),
                    edges=[],
                    directed=False
                )

            # Pipelines are converted to a directed network
            if isinstance(step, Pipeline):
                return Network(
                    nodes=[],
                    edges=zip(
                        map(networkify, list(step.steps.values())[:-1]),
                        map(networkify, list(step.steps.values())[1:])
                    ),
                    directed=True
                )

            # Wrapper models are handled recursively
            if isinstance(step, base.Wrapper):
                return Network(
                    nodes=[networkify(step._wrapper_model)],
                    edges=[],
                    directed=True,
                    name=type(step).__name__,
                    labelloc=step._labelloc
                )

            # Other steps are treated as strings
            return str(step)

        # Draw input
        net = Network(nodes=['x'], edges=[], directed=True)
        previous = 'x'

        # Draw each step
        for step in self.steps.values():
            current = networkify(step)
            net.link(previous, current)
            previous = current

        # Draw output
        net.link(previous, 'y')

        return net.draw()


class Network(collections.UserList):
    """An abstraction to help with drawing pipelines."""

    def __init__(self, nodes, edges, directed, name=None, labelloc=None):
        super().__init__()
        for node in nodes:
            self.append(node)
        self.edges = set()
        for link in edges:
            self.link(*link)
        self.directed = directed
        self.name = name
        self.labelloc = labelloc

    def append(self, a):
        if a not in self:
            super().append(a)

    def link(self, a, b):
        self.append(a)
        self.append(b)
        self.edges.add((self.index(a), self.index(b)))

    def draw(self):

        import graphviz

        G = graphviz.Digraph()

        drawn_subclusters = set()

        def draw_node(a):
            if isinstance(a, Network):
                for part in a:
                    draw_node(part)
            else:
                G.node(a)

        for a in self:
            draw_node(a)

        def draw_link(a, b):

            if isinstance(a, Network):

                # Connect the last part of a with b
                if a.directed:
                    draw_link(a[-1], b)
                # Connect each part of a with b
                else:
                    for part in a:
                        draw_link(part, b)

            elif isinstance(b, Network):

                # Connect the first part of b with a
                if b.directed:

                    if str(b) not in drawn_subclusters:

                        sub = b.draw()

                        # If the graph has a name, then we treat is as a cluster
                        if b.name is not None:
                            sub.attr(label=b.name, labelloc=b.labelloc)
                            sub.name = f'cluster_{b.name}'

                        G.subgraph(sub)

                        drawn_subclusters.add(str(b))

                    draw_link(a, b[0])

                # Connect each part of b with a
                else:
                    for part in b:
                        draw_link(a, part)

            else:
                G.edge(a, b)

        for a, b in self.edges:
            draw_link(self[a], self[b])

        return G
