# Copyright 2024 The Penzai Authors.
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

"""Lazy setup logic for adding PyTorch support to treescope."""

from __future__ import annotations

import keyword
import typing

import numpy as np
from penzai.treescope import context
from penzai.treescope import formatting_util
from penzai.treescope import ndarray_adapters
from penzai.treescope import renderer
from penzai.treescope import type_registries
from penzai.treescope.foldable_representation import basic_parts
from penzai.treescope.foldable_representation import common_structures
from penzai.treescope.foldable_representation import common_styles
from penzai.treescope.foldable_representation import foldable_impl
from penzai.treescope.foldable_representation import part_interface
from penzai.treescope.handlers import builtin_structure_handler

# pylint: disable=g-import-not-at-top
try:
  import torch
except ImportError:
  assert not typing.TYPE_CHECKING
  torch = None
# pylint: enable=g-import-not-at-top

show_dynamic_attributes: context.ContextualValue[bool] = (
    context.ContextualValue(
        module=__name__,
        qualname="show_dynamic_attributes",
        initial_value=True,
    )
)
"""Whether to inspect and show all non-private attributes of Torch modules.

If set to True, when rendering a Torch module, we will walk all of its
attributes (the entries in its `__dict__`) and render every attribute that does
not start with an underscore. If set to False, we will defer to the `extra_repr`
method instead, which matches default Torch repr behavior.
"""


def _truncate_and_copy(
    array_source: torch.Tensor,
    array_dest: np.ndarray,
    prefix_slices: tuple[slice, ...],
    remaining_edge_items_per_axis: tuple[int | None, ...],
) -> None:
  """Recursively copy values on the edges of a torch tensor into a numpy array.

  This function mutates the destination array in place, copying parts of input
  array into them, so that it contains a truncated versions of the original
  array.

  Args:
    array_source: Source array, which we will truncate.
    array_dest: Destination array, whose axis sizes will be either the same as
      `array_source` or of size `2 * edge_items + 1` depending on the
      truncation.
    prefix_slices: Prefix of slices for the source and destination.
    remaining_edge_items_per_axis: Number of edge items to keep for each axis,
      ignoring any axes whose slices are already computed in `source_slices`.
  """
  assert torch is not None, "PyTorch is not available."
  if not remaining_edge_items_per_axis:
    # Perform the base case slice.
    assert (
        len(prefix_slices) == len(array_source.shape) == len(array_dest.shape)
    )
    array_dest[prefix_slices] = array_source[prefix_slices].numpy()
  else:
    # Recursive step.
    axis = len(prefix_slices)
    edge_items = remaining_edge_items_per_axis[0]
    if edge_items is None:
      # Don't need to slice.
      _truncate_and_copy(
          array_source=array_source,
          array_dest=array_dest,
          prefix_slices=prefix_slices + (slice(None),),
          remaining_edge_items_per_axis=remaining_edge_items_per_axis[1:],
      )
    else:
      assert array_source.shape[axis] > 2 * edge_items
      _truncate_and_copy(
          array_source=array_source,
          array_dest=array_dest,
          prefix_slices=prefix_slices + (slice(None, edge_items),),
          remaining_edge_items_per_axis=remaining_edge_items_per_axis[1:],
      )
      _truncate_and_copy(
          array_source=array_source,
          array_dest=array_dest,
          prefix_slices=prefix_slices + (slice(-edge_items, None),),
          remaining_edge_items_per_axis=remaining_edge_items_per_axis[1:],
      )


class TorchTensorAdapter(ndarray_adapters.NDArrayAdapter[torch.Tensor]):
  """NDArray adapter for Torch tensors."""

  def get_axis_info_for_array_data(
      self, array: torch.Tensor
  ) -> tuple[ndarray_adapters.AxisInfo, ...]:
    infos = []
    for i, (size, name) in enumerate(zip(array.shape, array.names)):
      if name is None:
        infos.append(ndarray_adapters.PositionalAxisInfo(i, size))
      else:
        infos.append(
            ndarray_adapters.NamedPositionalAxisInfo(
                axis_logical_index=i, axis_name=name, size=size
            )
        )
    return tuple(infos)

  def get_array_data_with_truncation(
      self,
      array: torch.Tensor,
      mask: torch.Tensor | None,
      edge_items_per_axis: tuple[int | None, ...],
  ) -> tuple[np.ndarray, np.ndarray]:
    assert torch is not None, "PyTorch is not available."
    array = array.detach()

    if mask is None:
      mask = np.ones((1,) * array.ndim, dtype=bool)

    mask = torch.as_tensor(mask).detach()

    # Broadcast mask. (Note: Broadcasting a Numpy array does not copy data.)
    mask = torch.broadcast_to(mask, array.shape)

    if edge_items_per_axis == (None,) * array.ndim:
      # No truncation.
      return array.numpy(), mask.numpy()

    dest_shape = [
        size if edge_items is None else 2 * edge_items + 1
        for size, edge_items in zip(array.shape, edge_items_per_axis)
    ]
    array_dest = np.zeros(dest_shape, dtype=self.get_numpy_dtype(array))
    mask_dest = np.zeros(dest_shape, dtype=np.bool_)
    _truncate_and_copy(
        array_source=array,
        array_dest=array_dest,
        prefix_slices=(),
        remaining_edge_items_per_axis=edge_items_per_axis,
    )
    _truncate_and_copy(
        array_source=mask,
        array_dest=mask_dest,
        prefix_slices=(),
        remaining_edge_items_per_axis=edge_items_per_axis,
    )
    return array_dest, mask_dest

  def get_array_summary(self, array: torch.Tensor, fast: bool) -> str:
    assert torch is not None, "PyTorch is not available."
    ty = type(array)
    array = array.detach()
    typename = f"{ty.__module__}.{ty.__name__}"
    if typename == "torch.nn.parameter.Parameter":
      typename = "torch.nn.Parameter"
    output_parts = [f"{typename} "]

    output_parts.append(repr(array.dtype).removeprefix("torch."))
    name_parts = []
    for size, name in zip(array.shape, array.names):
      if name:
        name_parts.append(f"{name}:{size}")
      else:
        name_parts.append(f"{size}")
    if len(name_parts) == 1:
      output_parts.append("(" + name_parts[0] + ",)")
    else:
      output_parts.append("(" + ", ".join(name_parts) + ")")

    # Drop axis names.
    array = array.rename(None)
    size = np.prod(array.shape)
    if size > 0 and size < 100_000 and not fast:
      is_floating = array.dtype.is_floating_point
      is_bool = array.dtype == torch.bool
      is_integer = (
          not is_floating and not is_bool and not array.dtype.is_complex
      )

      if is_floating:
        isfinite = torch.isfinite(array)
        any_finite = torch.any(isfinite)
        inf_to_nan = torch.where(isfinite, array, torch.nan)
        mean = torch.nanmean(inf_to_nan)
        std = torch.nanmean(torch.square(inf_to_nan - mean))

        if any_finite:
          output_parts.append(f" ≈{float(mean):.2} ±{float(std):.2}")
          nanmin = torch.amin(torch.where(isfinite, array, torch.inf))
          nanmax = torch.amax(torch.where(isfinite, array, -torch.inf))
          output_parts.append(f" [≥{float(nanmin):.2}, ≤{float(nanmax):.2}]")

      if is_integer:
        output_parts.append(
            f" [≥{torch.amin(array):_d}, ≤{torch.amax(array):_d}]"
        )

      if is_floating or is_integer:
        ct_zero = torch.count_nonzero(array == 0)
        if ct_zero:
          output_parts.append(f" zero:{ct_zero:_d}")

        ct_nonzero = torch.count_nonzero(array)
        if ct_nonzero:
          output_parts.append(f" nonzero:{ct_nonzero:_d}")

      if is_floating:
        ct_nan = torch.count_nonzero(torch.isnan(array))
        if ct_nan:
          output_parts.append(f" nan:{ct_nan:_d}")

        ct_inf = torch.count_nonzero(torch.isposinf(array))
        if ct_inf:
          output_parts.append(f" inf:{ct_inf:_d}")

        ct_neginf = torch.count_nonzero(torch.isneginf(array))
        if ct_neginf:
          output_parts.append(f" -inf:{ct_neginf:_d}")

      if is_bool:
        ct_true = torch.count_nonzero(array)
        if ct_true:
          output_parts.append(f" true:{ct_true:_d}")

        ct_false = torch.count_nonzero(torch.logical_not(array))
        if ct_false:
          output_parts.append(f" false:{ct_false:_d}")

    return "".join(output_parts)

  def get_numpy_dtype(self, array: torch.Tensor) -> np.dtype:
    assert torch is not None, "PyTorch is not available."
    # Convert a zero-sized tensor to a numpy array to get its dtype.
    return torch.zeros((0,), dtype=array.dtype).numpy().dtype


def render_torch_tensors(
    node: torch.Tensor,
    path: str | None,
    subtree_renderer: renderer.TreescopeSubtreeRenderer,
) -> (
    part_interface.RenderableTreePart
    | part_interface.RenderableAndLineAnnotations
    | type(NotImplemented)
):
  """Renders a numpy array."""
  assert torch is not None, "PyTorch is not available."
  del subtree_renderer
  assert isinstance(node, torch.Tensor)
  adapter = TorchTensorAdapter()

  def _placeholder() -> part_interface.RenderableTreePart:
    return common_structures.fake_placeholder_foldable(
        common_styles.DeferredPlaceholderStyle(
            basic_parts.Text(adapter.get_array_summary(node, fast=True))
        ),
        extra_newlines_guess=8,
    )

  def _thunk(placeholder):
    # Is this array simple enough to render without a summary?
    node_repr = repr(node)
    if "\n" not in node_repr and "..." not in node_repr:
      if node_repr.startswith("tensor("):
        # Add module path, for consistency with other Treescope renderings.
        node_repr = f"torch.{node_repr}"
      rendering = basic_parts.Text(node_repr)
    else:
      if node_repr.count("\n") <= 15:
        if isinstance(placeholder, part_interface.FoldableTreeNode):
          default_expand_state = placeholder.get_expand_state()
        else:
          assert placeholder is None
          default_expand_state = part_interface.ExpandState.WEAKLY_EXPANDED
      else:
        # Always start big NDArrays in collapsed mode to hide irrelevant detail.
        default_expand_state = part_interface.ExpandState.COLLAPSED

      # Render it with a summary.
      summarized = adapter.get_array_summary(node, fast=False)
      rendering = common_structures.build_custom_foldable_tree_node(
          label=common_styles.AbbreviationColor(
              common_styles.CommentColorWhenExpanded(
                  basic_parts.siblings(
                      basic_parts.FoldCondition(
                          expanded=basic_parts.Text("# "),
                          collapsed=basic_parts.Text("<"),
                      ),
                      summarized,
                      basic_parts.FoldCondition(
                          collapsed=basic_parts.Text(">")
                      ),
                  )
              )
          ),
          contents=basic_parts.FoldCondition(
              expanded=basic_parts.IndentedChildren.build(
                  [basic_parts.Text(node_repr)]
              )
          ),
          path=path,
          expand_state=default_expand_state,
      ).renderable

    return rendering

  return basic_parts.RenderableAndLineAnnotations(
      renderable=foldable_impl.maybe_defer_rendering(
          main_thunk=_thunk, placeholder_thunk=_placeholder
      ),
      annotations=common_structures.build_copy_button(path),
  )


def render_torch_modules(
    node: torch.nn.Module,
    path: str | None,
    subtree_renderer: renderer.TreescopeSubtreeRenderer,
) -> (
    part_interface.RenderableTreePart
    | part_interface.RenderableAndLineAnnotations
    | type(NotImplemented)
):
  """Renders a torch module."""
  assert torch is not None, "PyTorch is not available."
  assert isinstance(node, torch.nn.Module)
  node_type = type(node)
  constructor = basic_parts.siblings(
      basic_parts.RoundtripCondition(roundtrip=basic_parts.Text("<")),
      common_structures.maybe_qualified_type_name(node_type),
      "(",
  )
  closing_suffix = basic_parts.siblings(
      ")",
      basic_parts.RoundtripCondition(roundtrip=basic_parts.Text(">")),
  )

  if hasattr(node, "__treescope_color__") and callable(
      node.__treescope_color__
  ):
    background_color, background_pattern = (
        builtin_structure_handler.parse_color_and_pattern(
            node.__treescope_color__(), node_type.__name__
        )
    )
  elif type(node) is torch.nn.Sequential:  # pylint: disable=unidiomatic-typecheck
    background_color = "#cdcdcd"
    background_pattern = "color-mix(in oklab, #cdcdcd 25%, white)"
  elif type(node).forward is torch.nn.Module.forward:
    # No implementation of forward. Don't color-code; this is probably a
    # container like ModuleList or ModuleDict.
    background_color = None
    background_pattern = None
  else:
    type_string = node_type.__module__ + "." + node_type.__qualname__
    background_color = formatting_util.color_from_string(type_string)
    background_pattern = None

  children = []
  prefers_expand = False
  attr_children = None
  has_attr_children_expander = False

  # Render constant attributes.
  if show_dynamic_attributes.get():
    attr_children = []
    key_order = [
        key
        for key in vars(node)
        if not key.startswith("_") and key != "training"
    ]
    if "training" in vars(node):
      key_order.append("training")
    for attr in key_order:
      value = vars(node)[attr]
      child_path = None if path is None else f"{path}.{attr}"
      attr_children.append(
          basic_parts.build_full_line_with_annotations(
              basic_parts.siblings_with_annotations(
                  f"{attr}=",
                  subtree_renderer(value, path=child_path),
                  ",",
                  basic_parts.FoldCondition(collapsed=basic_parts.Text(" ")),
              )
          )
      )
    if len(attr_children) <= 1:
      children.extend(attr_children)
    else:
      has_attr_children_expander = True
      children.append(
          common_structures.build_custom_foldable_tree_node(
              label=basic_parts.FoldCondition(
                  expanded=common_styles.CommentColor(
                      basic_parts.Text("# Attributes:")
                  ),
              ),
              contents=basic_parts.OnSeparateLines.build(attr_children),
              path=None,
              expand_state=part_interface.ExpandState.COLLAPSED,
          )
      )
  else:
    extra_repr = node.extra_repr()
    if extra_repr:
      if not extra_repr.strip().endswith(","):
        extra_repr = extra_repr + ", "
      if "\n" in extra_repr:
        children.append(
            basic_parts.OnSeparateLines.build(extra_repr.split("\n"))
        )
        prefers_expand = True
      else:
        children.append(basic_parts.Text(extra_repr))

  # Render parameters and buffers
  for group_name, group in (
      ("Parameters", node.named_parameters(recurse=False)),
      ("Buffers", node.named_buffers(recurse=False)),
  ):
    group = list(group)
    if group:
      children.append(
          basic_parts.FoldCondition(
              expanded=common_styles.CommentColor(
                  basic_parts.Text(f"# {group_name}:")
              )
          )
      )
      for name, value in group:
        child_path = None if path is None else f"{path}.{name}"
        children.append(
            basic_parts.build_full_line_with_annotations(
                basic_parts.siblings_with_annotations(
                    f"{name}=",
                    subtree_renderer(value, path=child_path),
                    ",",
                    basic_parts.FoldCondition(collapsed=basic_parts.Text(" ")),
                )
            )
        )

  # Render submodules.
  submodules = list(node.named_children())
  if submodules:
    children.append(
        basic_parts.FoldCondition(
            expanded=common_styles.CommentColor(
                basic_parts.Text("# Child modules:")
            )
        )
    )
    for name, submod in submodules:
      prefers_expand = True
      if name.isidentifier() and not keyword.iskeyword(name):
        child_path = None if path is None else f"{path}.{name}"
        keystr = f"{name}="
      else:
        child_path = f"{path}.get_submodule({repr(name)})"
        keystr = f"({name}): "
      children.append(
          basic_parts.build_full_line_with_annotations(
              basic_parts.siblings_with_annotations(
                  keystr,
                  subtree_renderer(submod, path=child_path),
                  ",",
                  basic_parts.FoldCondition(collapsed=basic_parts.Text(" ")),
              )
          )
      )

  # If there are only dynamic attributes, don't add the level of indirection.
  if has_attr_children_expander and len(children) == 1:
    children = attr_children

  # Heuristic: If a module doesn't have any submodules, mark it collapsed, to
  # match the behavior of PyTorch repr.
  if prefers_expand:
    expand_state = part_interface.ExpandState.WEAKLY_EXPANDED
  else:
    expand_state = part_interface.ExpandState.COLLAPSED

  return common_structures.build_foldable_tree_node_from_children(
      prefix=constructor,
      children=children,
      suffix=closing_suffix,
      path=path,
      background_color=background_color,
      background_pattern=background_pattern,
      expand_state=expand_state,
  )


def set_up_treescope():
  """Sets up treescope to render PyTorch objects."""
  if torch is None:
    raise RuntimeError(
        "Cannot set up PyTorch support in treescope: PyTorch cannot be"
        " imported."
    )
  type_registries.NDARRAY_ADAPTER_REGISTRY[torch.Tensor] = TorchTensorAdapter()
  type_registries.TREESCOPE_HANDLER_REGISTRY[torch.Tensor] = (
      render_torch_tensors
  )
  type_registries.TREESCOPE_HANDLER_REGISTRY[torch.nn.Module] = (
      render_torch_modules
  )