import chainer as ch

from . import header


class AbstractGCN(ch.Chain):

    def __init__(self, block_setting):
        """Initialize the NN.

        Args:
            block_setting: siml.setting.BlockSetting
                BlockSetting object.
        """

        super().__init__()
        nodes = block_setting.nodes
        with self.init_scope():
            self.chains = ch.ChainList(*[
                ch.links.Linear(n1, n2)
                for n1, n2 in zip(nodes[:-1], nodes[1:])])
        self.activations = [
            header.DICT_ACTIVATIONS[activation]
            for activation in block_setting.activations]
        self.dropout_ratios = [
            dropout_ratio for dropout_ratio in block_setting.dropouts]
        self.support_input_index = block_setting.support_input_index

    def __call__(self, x, supports):
        """Execute the NN's forward computation.

        Args:
            x: numpy.ndarray or cupy.ndarray
                Input of the NN.
            supports: List[chainer.util.CooMatrix]
                List of support inputs.
        Returns:
            y: numpy.ndarray of cupy.ndarray
                Output of the NN.
        """
        hs = ch.functions.stack([
            self._call_single(x_, supports_[self.support_input_index])
            for x_, supports_ in zip(x, supports)])
        return hs

    def _call_single(self, x, support):
        raise NotImplementedError


class GCN(AbstractGCN):
    """Graph Convolutional network according to
    https://arxiv.org/abs/1609.02907 .
    """

    def _call_single(self, x, support):
        h = x
        for link, dropout_ratio, activation in zip(
                self, self.dropout_ratios, self.activations):
            h = ch.functions.einsum('mf,gf->mg', h, link.W) + link.b
            h = ch.functions.dropout(h, ratio=dropout_ratio)
            h = activation(ch.functions.sparse_matmul(support, h))
        return h


class ResGCN(AbstractGCN):
    """Residual version of Graph Convolutional network.
    """

    def __init__(self, block_setting):
        super().__init__(block_setting)
        nodes = block_setting.nodes
        with self.init_scope():
            self.linear = ch.links.Linear(nodes[0], nodes[-1])

    def _call_single(self, x, support):
        h = x
        for link, dropout_ratio, activation in zip(
                self.chains, self.dropout_ratios, self.activations):
            h = ch.functions.einsum('mf,gf->mg', h, link.W) + link.b
            h = ch.functions.dropout(h, ratio=dropout_ratio)
            h = activation(ch.functions.sparse_matmul(support, h))
        return h + self.linear(x)
