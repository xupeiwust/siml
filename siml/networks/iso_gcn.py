
import einops
import torch

from .. import setting
from . import abstract_gcn
from . import identity
from . import mlp
from . import tensor_operations


class IsoGCN(abstract_gcn.AbstractGCN):
    """IsoGCN according to https://arxiv.org/abs/2005.06316 ."""

    @staticmethod
    def get_name():
        return 'iso_gcn'

    @staticmethod
    def accepts_multiple_inputs():
        return True

    def __init__(self, block_setting):
        self.is_first = True  # For block setting validation

        len_support = len(block_setting.support_input_indices)
        if len_support < 2:
            raise ValueError(
                'len(support_input_indices) should be larger than 1 '
                f"({len_support} found.)")

        self.create_subchain = block_setting.optional.get(
            'create_subchain', True)
        super().__init__(
            block_setting, create_subchain=False,
            multiple_networks=False, residual=block_setting.residual)
        if self.create_subchain:
            n_layer = len(self.block_setting.nodes) - 1
            if n_layer == 0:
                raise ValueError(
                    f"# of layers is zero for: {block_setting}")

            if n_layer == 1:
                self.subchains = torch.nn.ModuleList([
                    torch.nn.ModuleList([
                        torch.nn.Linear(
                            self.block_setting.nodes[0],
                            self.block_setting.nodes[-1],
                            bias=self.block_setting.bias)])])
                self.has_coefficient_network = False
            else:
                self.subchains = torch.nn.ModuleList([
                    torch.nn.ModuleList([
                        torch.nn.Linear(
                            self.block_setting.nodes[0],
                            self.block_setting.nodes[-1],
                            bias=False)])])
                self.coefficient_network = mlp.MLP(self.block_setting)
                print(f"Coefficient network created for: {block_setting.name}")
                self.has_coefficient_network = True
                self.contraction = tensor_operations.Contraction(
                    setting.BlockSetting())

        else:
            print(f"Skip subchain creation for: {block_setting.name}")
            self.subchains = [[identity.Identity(block_setting)]]
            self.has_coefficient_network = False

        self.dim = block_setting.optional.get('dim', 3)
        self.support_tensor_rank = block_setting.optional.get(
            'support_tensor_rank', 1)
        self.symmetric = block_setting.optional.get('symmetric', False)
        if self.symmetric:
            print(f"Output symmetric matrix for: {block_setting.name}")

        self.factor = block_setting.optional.get('factor', 1.)
        self.repeat = block_setting.optional.get(
            'repeat', 1)
        self.convergence_threshold = block_setting.optional.get(
            'convergence_threshold', None)
        print(f"Factor: {self.factor}")
        print(
            f"max repeat: {self.repeat}, "
            f"convergeence threshold: {self.convergence_threshold}")
        if self.repeat > 1:
            self.propagate_core = self._propagate_core_implicit
        else:
            self.propagate_core = self._propagate_core_explicit
        self.ah_w = block_setting.optional.get(
            'ah_w', False)
        if self.ah_w:
            print('Matrix multiplication mode: (AH) W')
        else:
            print('Matrix multiplication mode: A (HW)')

        if 'propagations' not in block_setting.optional:
            raise ValueError(
                f"Specify 'propagations' in optional for: {block_setting}")

        self.propagation_functions = self._create_propagation_functions()

        # Setting for Neumann BC
        if self.block_setting.input_names is not None:
            if len(self.block_setting.input_names) != 3:
                raise ValueError(
                    f"# of input names invalid for {self.block_setting}")
            self.has_neumann = True
            self._init_neumann()
        else:
            self.has_neumann = False
        return

    def _init_neumann(self):
        self.create_neumann_linear = self.block_setting.optional.get(
            'create_neumann_linear', False)
        self.neumann_factor = self.block_setting.optional.get(
            'neumann_factor', 1.)
        self.create_neumann_ratio = self.block_setting.optional.get(
            'create_neumann_ratio', False)

        if self.create_neumann_linear:
            self.neumann_linear = torch.nn.Linear(
                *self.subchains[0][0].weight.shape,
                bias=False)
        else:
            self.neumann_linear = self.subchains[0][0]

        if self.neumann_linear.bias is not None:
            raise ValueError(
                'IsoGCN with Neumann should have no bias: '
                f"{self.block_setting}")

        if self.create_neumann_ratio:
            self.neumann_ratio = torch.nn.Linear(1, 1, bias=False)

        return

    def _validate_block(self, x, supports):
        shape = x.shape
        self.x_tensor_rank \
            = len(shape) - 2  # (n_vertex, dim, dim, ..., n_feature)

        if len(supports) != self.dim**self.support_tensor_rank:
            raise ValueError(
                f"{self.dim**self.support_tensor_rank} ength of supports "
                f"expected (actual: {len(supports)} for: {self.block_setting}")

        if not self.create_subchain:
            return

        str_propagations = self.block_setting.optional['propagations']
        n_propagation = len(str_propagations)
        n_contraction = sum([
            s == 'contraction' for s in str_propagations])
        n_rank_raise_propagation = n_propagation - n_contraction
        estimated_output_tensor_rank = \
            self.x_tensor_rank - n_contraction + n_rank_raise_propagation

        if estimated_output_tensor_rank > 0 \
                and self.block_setting.activations[-1] != 'identity':
            raise ValueError(
                'Set identity activation for rank '
                f"{estimated_output_tensor_rank} output: {self.block_setting}")

        if self.has_coefficient_network:
            return

        if self.x_tensor_rank == 0:
            if estimated_output_tensor_rank > 0:
                # rank 0 -> rank k tensor
                if self.block_setting.bias and self.ah_w:
                    raise ValueError(
                        'Set bias = False for rank 0 -> k with (AH) W: '
                        f"{self.block_setting}")

        else:
            if estimated_output_tensor_rank == 0:
                # rank k -> rank 0 tensor
                if self.block_setting.bias and not self.ah_w:
                    raise ValueError(
                        'Set bias = False for rank k -> 0 with A (HW): '
                        f"{self.block_setting}")
            else:
                # rank k -> rank l tensor
                if self.block_setting.bias:
                    raise ValueError(
                        'Set bias = False for rank k -> l with A (HW): '
                        f"{self.block_setting}")
        return

    def _forward_single(self, x, *args, supports=None):
        if self.is_first:
            self._validate_block(x, supports)
            self.is_first = False
        if self.residual:
            shortcut = self.shortcut(x)
        else:
            shortcut = 0.
        h_res = self._propagate(x, supports)

        if self.has_neumann:
            directed_neumann = args[0]
            inversed_moment_tensors = args[1]
            h_res = self._add_neumann(
                h_res, directed_neumann=directed_neumann,
                inversed_moment_tensors=inversed_moment_tensors)

        if self.has_coefficient_network:
            if self.x_tensor_rank == 0:
                coeff = self.coefficient_network(x)
            else:
                coeff = self.coefficient_network(self.contraction(x))
            h_res = torch.einsum('i...f,if->i...f', h_res, coeff)

        if self.block_setting.activation_after_residual:
            h_res = self.activations[-1](h_res + shortcut)
        else:
            h_res = self.activations[-1](h_res) + shortcut

        return h_res

    def _add_neumann(self, grad, directed_neumann, inversed_moment_tensors):
        neumann = torch.einsum(
            'ikl,il...f->ik...f',
            inversed_moment_tensors[..., 0],
            self.neumann_linear(directed_neumann)) * self.neumann_factor
        if self.create_neumann_ratio:
            sigmoid_coeff = torch.sigmoid(self.coeff.weight[0, 0])
            return (sigmoid_coeff * grad + (1 - sigmoid_coeff) * neumann) * 2
        else:
            return grad + neumann

    def _propagate(self, x, support):
        h = x
        if not self.ah_w:
            # A (H W)
            h = self.subchains[0][0](h)

        h = self.propagate_core(h, support)

        if self.ah_w:
            # (A H) W
            h = self.subchains[0][0](h)

        if self.symmetric:
            h = (h + einops.rearrange(
                h, 'element x1 x2 feature -> element x2 x1 feature')) / 2

        h = torch.nn.functional.dropout(
            h, p=self.dropout_ratios[0], training=self.training)
        return h

    def _propagate_core_explicit(self, x, support):
        return self._apply_propagation_functions(x, support)

    def _propagate_core_implicit(self, x, support):
        h = x
        for _ in range(self.repeat):
            h_previous = h
            h = h + self._apply_propagation_functions(x, support)
            if self.convergence_threshold is not None:
                residual = torch.linalg.norm(
                    h - h_previous) / (torch.linalg.norm(h_previous) + 1.e-5)
                if residual < self.convergence_threshold:
                    break
        return h

    def _apply_propagation_functions(self, x, support):
        h = x
        for propagation_function in self.propagation_functions:
            h = propagation_function(h, support) * self.factor
        return h

    def _create_propagation_functions(self):
        str_propagations = self.block_setting.optional['propagations']
        propagation_functions = [
            self._create_propagation_function(str_propagation)
            for str_propagation in str_propagations]

        return propagation_functions

    def _create_propagation_function(self, str_propagation):
        if str_propagation == 'convolution':
            return self._convolution
        elif str_propagation == 'contraction':
            return self._contraction
        elif str_propagation == 'tensor_product':
            return self._tensor_product
        elif str_propagation == 'rotation':
            return self._rotation
        else:
            raise ValueError(
                f"Unexpected propagation method: {str_propagation}")

    def _calculate_dim(self, supports, n_vertex):
        dim, mod = divmod(supports.shape[0], n_vertex)
        if mod != 0:
            raise ValueError(
                'IsoGCN not supported for\n'
                f"    Sparse shape: {[s.shape for s in supports]}\n",
                f"    n_vertex: {n_vertex}")
        return dim

    def _tensor_product(self, x, supports):
        """Calculate tensor product G \\otimes x.

        Parameters
        ----------
        x: torch.Tensor
            [n_vertex, dim, dim, ..., n_feature]-shaped tensor.
                       ~~~~~~~~~~~~~~
                       tensor rank repetition
        supports: list[torch.Tensor]
            List of [n_vertex, n_vertex]-shaped sparse tensor.

        Returns
        -------
        y: torch.Tensor
            [n_vertex, dim, dim, ..., dim, n_feature]-shaped tensor.
                       ~~~~~~~~~~~~~~~~~~~
                       tensor rank+1 repetition
        """
        shape = x.shape
        dim = len(supports)
        tensor_rank = len(shape) - 2
        if tensor_rank == 0:
            h = self._convolution(x, supports)
        elif tensor_rank > 0:
            h = torch.stack([
                self._tensor_product(x[:, i_dim], supports)
                for i_dim in range(dim)], dim=-2)
        else:
            raise ValueError(f"Tensor shape invalid: {shape}")

        return h

    def _convolution(self, x, supports, top=True):
        """Calculate convolution G \\ast x.

        Parameters
        ----------
        x: torch.Tensor
            [n_vertex, n_feature]-shaped tensor.
        supports: list[torch.Tensor]
            List of [n_vertex, n_vertex]-shaped sparse tensor.

        Returns
        -------
        y: torch.Tensor
            [n_vertex, dim, n_feature]-shaped tensor.
        """
        if self.support_tensor_rank == 1:
            h = torch.stack([support.mm(x) for support in supports], axis=-2)
        elif self.support_tensor_rank == 2:
            n = x.shape[0]
            f = x.shape[-1]
            h = torch.reshape(
                torch.stack([support.mm(x) for support in supports], axis=-2),
                (n, self.dim, self.dim, f))
            if self.symmetric:
                h = (h + einops.rearrange(
                    h, 'element x1 x2 feature -> element x2 x1 feature')) / 2
        else:
            raise NotImplementedError

        return h

    def _contraction(self, x, supports):
        """Calculate contraction G \\cdot B. It calculates
        \\sum_l G_{i,j,k_1,k_2,...,l} H_{jk_1,k_2,...,l,f}

        Parameters
        ----------
        x: torch.Tensor
            [n_vertex, dim, dim, ..., n_feature]-shaped tensor.
                       ~~~~~~~~~~~~~~
                       tensor rank repetition
        supports: list[torch.Tensor]
            List of [n_vertex, n_vertex]-shaped sparse tensor.

        Returns
        -------
        y: torch.Tensor
            [n_vertex, dim, ..., n_feature]-shaped tensor.
                       ~~~~~~~~~
                       tensor rank - 1 repetition
        """
        shape = x.shape
        tensor_rank = len(shape) - 2
        if tensor_rank == self.support_tensor_rank:
            if self.support_tensor_rank == 1:
                return torch.sum(torch.stack([
                    supports[i_dim].mm(x[:, i_dim]) for i_dim
                    in range(self.dim)]), dim=0)
            elif self.support_tensor_rank == 2:
                # ijkm, jnmf -> iknf
                return torch.stack([
                    torch.stack([
                        torch.sum(torch.stack([
                            supports[self.dim*k+m].mm(x[:, n, m])
                            for m in range(self.dim)]), dim=0)
                        for n in range(self.dim)], dim=1)
                    for k in range(self.dim)], dim=1)
            else:
                raise ValueError
        elif tensor_rank > 1:
            return torch.stack([
                self._contraction(x[:, i_dim], supports)
                for i_dim in range(self.dim)], dim=1)
        else:
            raise ValueError(f"Tensor rank is 0 (shape: {shape})")

    def _rotation(self, x, supports):
        """Calculate rotation G \\times x.

        Parameters
        ----------
        x: torch.Tensor
            [n_vertex, dim, n_feature]-shaped tensor.
        supports: list[torch.Tensor]
            List of [n_vertex, n_vertex]-shaped sparse tensor.

        Returns
        -------
        y: torch.Tensor
            [n_vertex, dim, n_feature]-shaped tensor.
        """
        shape = x.shape
        dim = len(supports)
        tensor_rank = len(shape) - 2
        if tensor_rank != 1:
            raise ValueError(f"Tensor shape invalid: {shape}")
        if dim != 3:
            raise ValueError(f"Invalid dimension: {dim}")
        h = torch.stack([
                supports[1].mm(x[:, 2]) - supports[2].mm(x[:, 1]),
                supports[2].mm(x[:, 0]) - supports[0].mm(x[:, 2]),
                supports[0].mm(x[:, 1]) - supports[1].mm(x[:, 0]),
            ], dim=-2)

        return h
