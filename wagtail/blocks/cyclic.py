"""
Everything that makes cyclic / self-referential block definitions work, in one place.

A cyclic block graph (a block that, through a chain of children, refers back to
itself) breaks the block system's tree assumption in three ways, each handled here:

- **Expressing the cycle** — `BlockReference`, a lazy proxy resolved on first access,
  and `BlockDict`, the child mapping that resolves references transparently.
- **Walking the cycle** — `CycleGuard` / `guard_full_graph_method`, applied to the
  full-graph methods (`check` / `defer_required_validation` /
  `restore_deferred_validation`) so a re-entered node terminates instead of recursing.
- **Rejecting the unsupported shape** — `has_struct_only_cycle`, used by
  `StructBlock.check()` to flag a cycle that never passes through a sequence block (it
  would have no finite default).

Every walk keys on ``Block._definition_id`` (a stable per-node identity) rather than
``id()``, so the fresh instances a migration lookup produces for one node are
recognised as the same node. Block and BaseStructBlock are imported lazily inside the
functions that need them, to keep this module free of import cycles with base.py.
"""

import collections
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from importlib import import_module


class CycleGuard:
    """
    Re-entry tracker for one operation that walks the block-definition graph. Keyed by
    node identity (``Block._definition_id``). The set of in-progress keys lives in a
    ContextVar, so a block reached recursively shares the walk with its ancestors.

    ``visit_once=True`` keeps each key for the whole walk, so a node is handled once
    even when reached by several paths (full-graph methods such as ``check``).
    ``visit_once=False`` drops the key when the call returns, so only keys on the
    current recursion stack short-circuit (coinductive equality).
    """

    def __init__(self, name, *, visit_once):
        self._var = ContextVar("block_cycle_%s" % name, default=None)
        self._visit_once = visit_once

    @contextmanager
    def entered(self, key):
        seen = self._var.get()
        token = None
        if seen is None:
            seen = set()
            token = self._var.set(seen)
        try:
            if key in seen:
                yield True
            else:
                seen.add(key)
                try:
                    yield False
                finally:
                    if not self._visit_once:
                        seen.discard(key)
        finally:
            if token is not None:
                self._var.reset(token)


# One guard per full-graph operation, created on demand. Methods that walk the stored
# value rather than the definition are not guarded: a cycle through a sequence block
# (ListBlock/StreamBlock) terminates via that block's empty default.
_full_graph_guards = {}


def guard_full_graph_method(on_reentry=None):
    """
    Guard a method that walks the whole block-definition graph against infinite recursion
    on a cyclic graph: re-entering an already-visited node returns ``on_reentry`` instead
    of recursing. Applied automatically by ``Block.__init_subclass__``.
    """

    def decorator(method):
        guard = _full_graph_guards.setdefault(
            method.__name__, CycleGuard(method.__name__, visit_once=True)
        )

        @wraps(method)
        def wrapped(self, *args, **kwargs):
            with guard.entered(self._definition_id) as reentered:
                return on_reentry if reentered else method(self, *args, **kwargs)

        wrapped._is_guarded = True
        return wrapped

    return decorator


# Equality of a cyclic block graph compares coinductively (see reference_eq).
_reference_eq_guard = CycleGuard("reference_eq", visit_once=False)


def _resolve_target(ref):
    from wagtail.blocks.base import Block

    if isinstance(ref, str):
        module_name, _, class_name = ref.rpartition(".")
        ref = getattr(import_module(module_name), class_name)
    if isinstance(ref, type):
        return ref()
    if callable(ref):
        result = ref()
        return result if isinstance(result, Block) else result()
    raise TypeError(
        "BlockReference expected a callable or dotted import path; got %r." % (ref,)
    )


def reference_eq(reference, other):
    """
    Equality for a BlockReference: equal to whatever it resolves to. Every cycle in a
    block graph runs through a reference, so this is where comparison of two cyclic
    graphs is kept finite — coinductively: the (node, node) pairs currently on the
    comparison stack short-circuit to "equal".
    """
    from wagtail.blocks.base import Block

    if isinstance(other, BlockReference):
        other = other.resolve()
    if not isinstance(other, Block):
        return NotImplemented

    resolved = reference.resolve()
    pair = (resolved._definition_id, other._definition_id)
    with _reference_eq_guard.entered(pair) as reentered:
        return True if reentered else resolved == other


class BlockReference:
    """
    A lazy proxy for a block, declared as a callable or dotted path and resolved on first
    access. Enables forward references and cyclic block graphs — the referenced class
    need not exist at declaration time::

        class AccordionBlock(blocks.StructBlock):
            content = blocks.BlockReference(lambda: ContentStreamBlock)
    """

    def __init__(self, ref):
        from wagtail.blocks.base import Block

        self._ref = ref
        self._resolved = None
        self.name = ""
        # Share Block's global creation counter so class-level references sort correctly
        # alongside Block instances in the declarative metaclass.
        self.creation_counter = Block.creation_counter
        Block.creation_counter += 1

    def set_name(self, name):
        self.name = name

    def resolve(self):
        if self._resolved is None:
            self._resolved = _resolve_target(self._ref)
            if self.name:
                self._resolved.set_name(self.name)
        return self._resolved

    def __eq__(self, other):
        return reference_eq(self, other)

    # Defining __eq__ makes this unhashable by default, matching Block.
    __hash__ = None


class BlockDict(collections.OrderedDict):
    """
    The mapping used for a container's ``child_blocks``. Entries are normally ``Block``
    instances but may be ``BlockReference`` instances for forward / cyclic references; a
    reference is resolved to a real block — named, and memoised in place — the first time
    it is read. Apart from that lazy resolution on read, it behaves as a normal
    ``OrderedDict``.
    """

    class ValuesView(collections.abc.ValuesView):
        def __iter__(self):
            for key in self._mapping:
                yield self._mapping[key]  # triggers __getitem__ → lazy resolution

    class ItemsView(collections.abc.ItemsView):
        def __iter__(self):
            for key in self._mapping:
                yield key, self._mapping[key]

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if isinstance(value, BlockReference):
            value = value.resolve()
            value.set_name(key)
            super().__setitem__(key, value)
        return value

    def update(self, other=(), **kwargs):
        # Copy raw values (including unresolved references) without triggering resolution
        # via __getitem__, so the metaclass MRO walk can build base_blocks before the
        # referenced classes exist.
        if hasattr(other, "keys"):
            for key in other.keys():
                collections.OrderedDict.__setitem__(
                    self, key, collections.OrderedDict.__getitem__(other, key)
                )
        else:
            for key, value in other:
                collections.OrderedDict.__setitem__(self, key, value)
        for key, value in kwargs.items():
            collections.OrderedDict.__setitem__(self, key, value)

    def copy(self):
        result = type(self)()
        for key in self:
            collections.OrderedDict.__setitem__(
                result, key, collections.OrderedDict.__getitem__(self, key)
            )
        return result

    def values(self):
        return self.ValuesView(self)

    def items(self):
        return self.ItemsView(self)

    def get(self, key, default=None):
        return self[key] if key in self else default


def has_struct_only_cycle(struct_block):
    """
    True if ``struct_block`` can reach itself through a chain of StructBlocks only.

    StructBlock is the only block whose ``get_default()`` descends into every child, so
    a cycle of nothing but StructBlocks has no finite default and could not be rendered;
    any non-StructBlock on the path (ListBlock/StreamBlock) has an empty default that
    breaks the cycle. (A custom block that overrides ``get_default()`` to descend would
    not be detected here.)
    """
    from wagtail.blocks.struct_block import BaseStructBlock

    start_id = struct_block._definition_id

    def reaches_start(block, visited_ids):
        for child in block.child_blocks.values():
            if not isinstance(child, BaseStructBlock):
                continue
            child_id = child._definition_id
            if child_id == start_id:
                return True
            if child_id not in visited_ids and reaches_start(
                child, visited_ids | {child_id}
            ):
                return True
        return False

    return reaches_start(struct_block, frozenset())
