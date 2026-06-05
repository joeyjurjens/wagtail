from collections import defaultdict
from importlib import import_module

from wagtail.blocks.base import BlockReference


class BlockDefinitionLookup:
    """
    A utility for constructing StreamField Block objects in migrations, starting from
    a compact representation that avoids repeating the same definition whenever a
    block is re-used in multiple places over the block definition tree.

    The underlying data is a dict of block definitions, such as:
    ```
    {
        0: ("wagtail.blocks.CharBlock", [], {"required": True}),
        1: ("wagtail.blocks.RichTextBlock", [], {}),
        2: ("wagtail.blocks.StreamBlock", [
            [
                ("heading", 0),
                ("paragraph", 1),
            ],
        ], {}),
    }
    ```

    where each definition is a tuple of (module_path, args, kwargs) similar to that
    returned by `deconstruct` - with the difference that any block objects appearing
    in args / kwargs may be substituted with an index into the lookup table that
    points to that block's definition. Any block class that wants to support such
    substitutions should implement a static/class method
    `construct_from_lookup(lookup, *args, **kwargs)`, where `lookup` is
    the `BlockDefinitionLookup` instance. The method should return a block instance
    constructed from the provided arguments (after performing any lookups).
    """

    def __init__(self, blocks):
        self.blocks = blocks
        self.block_classes = {}
        self._instances = {}
        self._building = set()
        self._cycle_participants = set()

    def get_block(self, index):
        # Return a cached instance — only cyclic blocks are cached.
        if index in self._instances:
            return self._instances[index]

        # We're already building this index — a cycle. Return a lazy BlockReference
        # so construction can complete; it resolves to the finished instance later.
        if index in self._building:
            self._cycle_participants.add(index)
            return BlockReference(lambda i=index: self._instances[i])

        self._building.add(index)
        path, args, kwargs = self.blocks[index]
        try:
            cls = self.block_classes[path]
        except KeyError:
            module_name, class_name = path.rsplit(".", 1)
            module = import_module(module_name)
            cls = self.block_classes[path] = getattr(module, class_name)
        block = cls.construct_from_lookup(self, *args, **kwargs)
        self._building.discard(index)

        # Cache only blocks that took part in a cycle, so the BlockReference above
        # can close onto a single shared instance.
        if index in self._cycle_participants:
            self._instances[index] = block

        return block


class BlockDefinitionLookupBuilder:
    """
    Helper for constructing the lookup data used by BlockDefinitionLookup
    """

    def __init__(self):
        self.blocks = []

        # Index of each block we have fully added, keyed by id(block), so the same block
        # instance re-used in several places is only stored once. Using id() is safe here
        # because all block instances are kept alive by their parent blocks for the entire
        # lifetime of the builder, so id values cannot be reused.
        self.block_indexes_by_identity = {}

        # Blocks whose deconstruction is in progress, keyed by id(block). The value is the
        # slot reserved for the block (or None if nothing has needed it yet). Used to break
        # cyclic graphs: a back-reference to a block still being deconstructed reserves its
        # slot here instead of recursing forever.
        self.pending_block_indexes = {}

        # Lookup of already-stored definitions for structural deduplication. The
        # deconstructed tuples can be compared for equality but not hashed, so we bucket
        # them by their first element (the module path) and keep a list of
        # (index, deconstructed_tuple) pairs per bucket.
        self.block_indexes_by_type = defaultdict(list)

    def add_block(self, block):
        """
        Add a block to the lookup table, returning an index that can be used to refer to
        it. Three cases:

        1. the block was already fully added — return its existing index;
        2. the block is currently being deconstructed and something refers back to it (a
           cycle) — reserve a slot for it now and return that index, breaking the recursion;
        3. first encounter — deconstruct it (which recurses into its children, possibly
           hitting case 2), then fill the reserved slot if a back-reference made one, or
           else deduplicate structurally / append a new slot.
        """
        identity = id(block)

        # Case 1: already fully added.
        if identity in self.block_indexes_by_identity:
            return self.block_indexes_by_identity[identity]

        # Case 2: a back-reference to a block still being deconstructed. Reserve a real slot
        # for it now (a placeholder, filled in when its own add_block call completes).
        if identity in self.pending_block_indexes:
            reserved_index = self.pending_block_indexes[identity]
            if reserved_index is None:
                reserved_index = len(self.blocks)
                self.blocks.append(None)
                self.pending_block_indexes[identity] = reserved_index
            return reserved_index

        # Case 3: first encounter. Mark as pending (no slot yet), then deconstruct — which
        # recurses into children and may reserve our slot via case 2.
        self.pending_block_indexes[identity] = None
        deconstructed = block.deconstruct_with_lookup(self)
        reserved_index = self.pending_block_indexes.pop(identity)

        block_indexes = self.block_indexes_by_type[deconstructed[0]]
        if reserved_index is None:
            # Nothing referred back to us, so we may reuse an identical definition.
            for existing_index, existing_deconstructed in block_indexes:
                if existing_deconstructed == deconstructed:
                    self.block_indexes_by_identity[identity] = existing_index
                    return existing_index
            index = len(self.blocks)
            self.blocks.append(deconstructed)
        else:
            # A back-reference reserved this slot (a cycle); fill it in, without dedup.
            index = reserved_index
            self.blocks[index] = deconstructed

        self.block_indexes_by_identity[identity] = index
        block_indexes.append((index, deconstructed))
        return index

    def get_lookup_as_dict(self):
        return dict(enumerate(self.blocks))
