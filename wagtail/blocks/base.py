import collections
import contextvars
import itertools
import json
import re
from contextlib import contextmanager
from functools import lru_cache
from importlib import import_module

from django import forms
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.utils.encoding import force_str
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.text import capfirst

from wagtail.admin.staticfiles import versioned_static
from wagtail.admin.telepath import Adapter, JSContext
from wagtail.admin.telepath import register as register_telepath_adapter
from wagtail.utils.templates import template_is_overridden

__all__ = [
    "BaseBlock",
    "Block",
    "BoundBlock",
    "DeclarativeSubBlocksMetaclass",
    "BlockWidget",
    "BlockField",
    "LazyBlock",
]


# =========================================
# Top-level superclasses and helper objects
# =========================================


class BaseBlock(type):
    def __new__(mcs, name, bases, attrs):
        meta_class = attrs.pop("Meta", None)

        cls = super().__new__(mcs, name, bases, attrs)

        # Get all the Meta classes from all the bases
        meta_class_bases = [meta_class] + [
            getattr(base, "_meta_class", None) for base in bases
        ]
        meta_class_bases = tuple(filter(bool, meta_class_bases))
        cls._meta_class = type(str(name + "Meta"), meta_class_bases, {})

        return cls


class Block(metaclass=BaseBlock):
    name = ""
    creation_counter = 0
    definition_registry = {}

    TEMPLATE_VAR = "value"
    DEFAULT_PREVIEW_TEMPLATE = "wagtailcore/shared/block_preview.html"

    class Meta:
        label = None
        icon = "placeholder"
        classname = None
        form_attrs = None
        group = ""

    # Attributes of Meta which can legally be modified after the block has been instantiated.
    # Used to implement __eq__. label is not included here, despite it technically being mutable via
    # set_name, since its value must originate from either the constructor arguments or set_name,
    # both of which are captured by the equality test, so checking label as well would be redundant.
    MUTABLE_META_ATTRIBUTES = []

    def __new__(cls, *args, **kwargs):
        # adapted from django.utils.deconstruct.deconstructible; capture the arguments
        # so that we can return them in the 'deconstruct' method
        obj = super().__new__(cls)
        obj._constructor_args = (args, kwargs)
        return obj

    def __init__(self, **kwargs):
        if "classname" in self._constructor_args[1]:
            # Adding this so that migrations are not triggered
            # when form_classname is used instead of classname
            # in the initialisation of the FieldBlock
            classname = self._constructor_args[1].pop("classname")
            self._constructor_args[1].setdefault("form_classname", classname)

        self.meta = self._meta_class()

        for attr, value in kwargs.items():
            setattr(self.meta, attr, value)

        # Increase the creation counter, and save our local copy.
        self.creation_counter = Block.creation_counter
        Block.creation_counter += 1
        self.definition_prefix = "blockdef-%d" % self.creation_counter
        Block.definition_registry[self.definition_prefix] = self

        self.label = self.meta.label or ""
        self.is_deferred_validation = False
        """
        Indicates whether this block is currently in a state where any validation
        that is not required for saving a draft should be deferred.
        """

    @classmethod
    def construct_from_lookup(cls, lookup, *args, **kwargs):
        """
        See `wagtail.blocks.definition_lookup.BlockDefinitionLookup`.
        Construct a block instance from the provided arguments, using the given BlockDefinitionLookup
        object to perform any necessary lookups.
        """
        # In the base implementation, no lookups take place - args / kwargs are passed
        # on to the constructor as-is
        return cls(*args, **kwargs)

    def set_name(self, name):
        self.name = name
        if not self.meta.label:
            self.label = capfirst(force_str(name).replace("_", " "))

    def set_meta_options(self, opts):
        """
        Update this block's meta options (out of the ones designated as mutable) from the given dict.
        Used by the StreamField constructor to pass on kwargs that are to be handled by the block,
        since the block object has already been created by that point, e.g.:
        body = StreamField(SomeStreamBlock(), max_num=5)
        """
        for attr, value in opts.items():
            if attr in self.MUTABLE_META_ATTRIBUTES:
                setattr(self.meta, attr, value)
            else:
                raise TypeError(
                    "set_meta_options received unexpected option: %r" % attr
                )

    def value_from_datadict(self, data, files, prefix):
        raise NotImplementedError("%s.value_from_datadict" % self.__class__)

    def value_omitted_from_data(self, data, files, name):
        """
        Used only for top-level blocks wrapped by BlockWidget (i.e.: typically only StreamBlock)
        to inform ModelForm logic on Django >=1.10.2 whether the field is absent from the form
        submission (and should therefore revert to the field default).
        """
        return name not in data

    def bind(self, value, prefix=None, errors=None):
        """
        Return a BoundBlock which represents the association of this block definition with a value
        and a prefix (and optionally, a ValidationError to be rendered).
        BoundBlock primarily exists as a convenience to allow rendering within templates:
        bound_block.render() rather than blockdef.render(value, prefix) which can't be called from
        within a template.
        """
        return BoundBlock(self, value, prefix=prefix, errors=errors)

    def _evaluate_callable(self, value):
        return value() if callable(value) else value

    def get_default(self):
        """
        Return this block's default value (conventionally found in self.meta.default),
        converted to the value type expected by this block. If the default is a callable
        (e.g. a function), it will be evaluated at runtime. This caters for
        the case where that value type is not something that can be expressed statically at
        model definition time (e.g. something like StructValue which incorporates a
        pointer back to the block definition object).
        """
        default = self._evaluate_callable(getattr(self.meta, "default", None))
        return self.normalize(default)

    def defer_required_validation(self):
        """
        Defer any validation that is not required when saving a draft, such as by
        setting ``required = False`` on child blocks. The corresponding restoration
        logic should be implemented in :meth:`restore_deferred_validation`.

        Subclasses that implement this method should also call
        ``super().defer_required_validation()``, to ensure the parent's deferred
        validation logic is also applied.
        """
        self.is_deferred_validation = True

    def clean(self, value):
        """
        Validate value and return a cleaned version of it, or throw a :class:`~django.core.exceptions.ValidationError` if validation fails.

        To determine whether to defer any validation that is not required for saving a
        draft, the :attr:`is_deferred_validation` attribute can be checked.

        For more details on how to implement custom validation logic, refer to
        :ref:`streamfield_validation`.
        """
        return value

    def restore_deferred_validation(self):
        """
        Restore any validation that was deferred by :meth:`defer_required_validation`.

        Subclasses that implement this method should also call
        ``super().restore_deferred_validation()``, to ensure the parent's deferred
        validation logic is also restored.
        """
        self.is_deferred_validation = False

    def clean_deferred(self, value):
        """
        Wraps :meth:`clean` with :meth:`defer_required_validation` and
        :meth:`restore_deferred_validation`, so that any validation that is not
        required for saving a draft can be deferred.

        This is only called on the top-level block of a StreamField (which is
        typically a StreamBlock). Instead of calling ``clean_deferred`` on child
        blocks, the defer/restore logic should be propagated to child blocks, which
        means the child blocks' ``clean()`` methods will be called with the deferred
        validation in place.
        """
        self.defer_required_validation()
        try:
            return self.clean(value)
        finally:
            self.restore_deferred_validation()

    def normalize(self, value):
        """
        Given a value for any acceptable type for this block (e.g. string or RichText for a RichTextBlock;
        dict or StructValue for a StructBlock), return a value of the block's native type (e.g. RichText
        for RichTextBlock, StructValue for StructBlock). In simple cases this will return the value
        unchanged.
        """
        return value

    def to_python(self, value):
        """
        Convert 'value' from a simple (JSON-serialisable) value to a (possibly complex) Python value to be
        used in the rest of the block API and within front-end templates . In simple cases this might be
        the value itself; alternatively, it might be a 'smart' version of the value which behaves mostly
        like the original value but provides a native HTML rendering when inserted into a template; or it
        might be something totally different (e.g. an image chooser will use the image ID as the clean
        value, and turn this back into an actual image object here).

        For blocks that are usable at the top level of a StreamField, this must also accept any type accepted
        by normalize. (This is because Django calls `Field.to_python` from `Field.clean`.)
        """
        return value

    def bulk_to_python(self, values):
        """
        Apply the to_python conversion to a list of values. The default implementation simply
        iterates over the list; subclasses may optimise this, e.g. by combining database lookups
        into a single query.
        """
        return [self.to_python(value) for value in values]

    def get_prep_value(self, value):
        """
        The reverse of to_python; convert the python value into JSON-serialisable form.
        """
        return value

    def get_form_state(self, value):
        """
        Convert a python value for this block into a JSON-serialisable representation containing
        all the data needed to present the value in a form field, to be received by the block's
        client-side component. Examples of where this conversion is not trivial include rich text
        (where it needs to be supplied in a format that the editor can process, e.g. ContentState
        for Draftail) and page / image / document choosers (where it needs to include all displayed
        data for the selected item, such as title or thumbnail).
        """
        return value

    def get_context(self, value, parent_context=None):
        """
        Return a dict of context variables (derived from the block ``value`` and combined with the
        ``parent_context``) to be used as the template context when rendering this value through a
        template. See :ref:`the usage example <streamfield_get_context>` for more details.
        """

        context = parent_context or {}
        context.update(
            {
                "self": value,
                self.TEMPLATE_VAR: value,
            }
        )
        return context

    def get_template(self, value=None, context=None):
        """
        Return the template to use for rendering the block if specified.
        This method allows for dynamic templates based on the block instance and a given ``value``.
        See :ref:`the usage example <streamfield_get_template>` for more details.
        """
        return getattr(self.meta, "template", None)

    def render(self, value, context=None):
        """
        Return a text rendering of 'value', suitable for display on templates. By default, this will
        use a template (with the passed context, supplemented by the result of get_context) if a
        'template' property is specified on the block, and fall back on render_basic otherwise.
        """
        template = self.get_template(value, context=context)
        if not template:
            return self.render_basic(value, context=context)

        if context is None:
            new_context = self.get_context(value)
        else:
            new_context = self.get_context(value, parent_context=dict(context))

        return mark_safe(render_to_string(template, new_context))

    def get_preview_context(self, value, parent_context=None):
        """
        Return a dict of context variables to be used as the template context
        when rendering the block's preview. The ``value`` argument is the value
        returned by :meth:`get_preview_value`. The ``parent_context`` argument
        contains the following variables:

        - ``request``: The current request object.
        - ``block_def``: The block instance.
        - ``block_class``: The block class.
        - ``bound_block``: A ``BoundBlock`` instance representing the block and its value.

        If :ref:`the global preview template <streamfield_global_preview_template>`
        is used, the block will be rendered as the main content using
        ``{% include_block %}``, which in turn uses :meth:`get_context`. As a
        result, the context returned by this method will be available as the
        ``parent_context`` for ``get_context()`` when the preview is rendered.
        """
        # NOTE: see StreamFieldBlockPreview.base_context for the context variables
        # that can be documented.
        return parent_context or {}

    def get_preview_template(self, value, context=None):
        """
        Return the template to use for rendering the block's preview. The ``value``
        argument is the value returned by :meth:`get_preview_value`. The ``context``
        argument contains the variables listed for the ``parent_context`` argument
        of :meth:`get_preview_context` above (and not the context returned by that
        method itself).

        Note that the preview template is used to render a complete HTML page of
        the preview, not just an HTML fragment for the block. The method returns
        the ``preview_template`` attribute from the block's options if provided,
        and falls back to
        :ref:`the global preview template <streamfield_global_preview_template>`
        otherwise.

        If the global preview template is used, the block will be rendered as the
        main content using ``{% include_block %}``, which in turn uses
        :meth:`get_template`.
        """
        return (
            getattr(self.meta, "preview_template", None)
            or self.DEFAULT_PREVIEW_TEMPLATE
        )

    def get_preview_value(self):
        """
        Return the placeholder value that will be used for rendering the block's
        preview. By default, the value is the ``preview_value`` from the block's
        options if provided. If it's a callable, it will be evaluated at runtime.
        If ``preview_value`` is not provided, the ``default`` is used as fallback.
        This method can also be overridden to provide a dynamic preview value.
        """
        if hasattr(self.meta, "preview_value"):
            value = self._evaluate_callable(self.meta.preview_value)
            return self.normalize(value)
        return self.get_default()

    @cached_property
    def _has_default(self):
        return getattr(self.meta, "default", None) is not None

    @cached_property
    def is_previewable(self):
        """
        Determine whether the block is previewable in the block picker. By
        default, it automatically detects when a custom template is used or the
        :ref:`the global preview template <streamfield_global_preview_template>`
        is overridden and a preview value is provided. If the block is
        previewable by other means, override this property to return ``True``.
        To turn off previews for the block, set it to ``False``.
        """
        has_specific_template = (
            hasattr(self.meta, "preview_template")
            or self.__class__.get_preview_template is not Block.get_preview_template
        )
        has_preview_value = (
            hasattr(self.meta, "preview_value")
            or self._has_default
            or self.__class__.get_preview_context is not Block.get_preview_context
            or self.__class__.get_preview_value is not Block.get_preview_value
        )
        has_global_template = template_is_overridden(
            self.DEFAULT_PREVIEW_TEMPLATE,
            "templates",
        )
        return has_specific_template or (has_preview_value and has_global_template)

    def get_description(self):
        """
        Return the description of the block to be shown to editors as part of the preview.
        For :ref:`field block types <field_block_types>`, it will fall back to
        ``help_text`` if not provided.
        """
        return getattr(self.meta, "description", "")

    def get_api_representation(self, value, context=None):
        """
        Can be used to customise the API response and defaults to the value returned by get_prep_value.
        """
        return self.get_prep_value(value)

    def render_basic(self, value, context=None):
        """
        Return a text rendering of 'value', suitable for display on templates. render() will fall back on
        this if the block does not define a 'template' property.
        """
        return force_str(value)

    def get_searchable_content(self, value):
        """
        Returns a list of strings containing text content within this block to be used in a search engine.
        """
        return []

    def extract_references(self, value):
        return []

    def get_block_by_content_path(self, value, path_elements):
        """
        Given a list of elements from a content path, retrieve the block at that path
        as a BoundBlock object, or None if the path does not correspond to a valid block.
        """
        # In the base case, where a block has no concept of children, the only valid path is
        # the empty one (which refers to the current block).
        if path_elements:
            return None
        else:
            return self.bind(value)

    def check(self, **kwargs):
        """
        Hook for the Django system checks framework -
        returns a list of django.core.checks.Error objects indicating validity errors in the block
        """
        return []

    def _check_name(self, **kwargs):
        """
        Helper method called by container blocks as part of the system checks framework,
        to validate that this block's name is a valid identifier.
        (Not called universally, because not all blocks need names)
        """
        errors = []
        if not self.name:
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    hint="Block name cannot be empty",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        if " " in self.name:
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    hint="Block names cannot contain spaces",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        if "-" in self.name:
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    "Block names cannot contain dashes",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        if self.name and self.name[0].isdigit():
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    "Block names cannot begin with a digit",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        if not errors and not re.match(r"^[_a-zA-Z][_a-zA-Z0-9]*$", self.name):
            errors.append(
                checks.Error(
                    "Block name %r is invalid" % self.name,
                    "Block names should follow standard Python conventions for "
                    "variable names: alphanumeric and underscores, and cannot "
                    "begin with a digit",
                    obj=kwargs.get("field", self),
                    id="wagtailcore.E001",
                )
            )

        return errors

    def id_for_label(self, prefix):
        """
        Return the ID to be used as the 'for' attribute of <label> elements that refer to this block,
        when the given field prefix is in use. Return None if no 'for' attribute should be used.
        """
        return None

    @property
    def required(self):
        """
        Flag used to determine whether labels for this block should display a 'required' asterisk.
        False by default, since Block does not provide any validation of its own - it's up to subclasses
        to define what required-ness means.
        """
        return False

    @cached_property
    def canonical_module_path(self):
        """
        Return the module path string that should be used to refer to this block in migrations.
        """
        # adapted from django.utils.deconstruct.deconstructible
        module_name = self.__module__
        name = self.__class__.__name__

        # Make sure it's actually there and not an inner class
        module = import_module(module_name)
        if not hasattr(module, name):
            raise ValueError(
                "Could not find object %s in %s.\n"
                "Please note that you cannot serialize things like inner "
                "classes. Please move the object into the main module "
                "body to use migrations.\n" % (name, module_name)
            )

        # if the module defines a DECONSTRUCT_ALIASES dictionary, see if the class has an entry in there;
        # if so, use that instead of the real path
        try:
            return module.DECONSTRUCT_ALIASES[self.__class__]
        except (AttributeError, KeyError):
            return f"{module_name}.{name}"

    def deconstruct(self):
        return (
            self.canonical_module_path,
            self._constructor_args[0],
            self._constructor_args[1],
        )

    def deconstruct_with_lookup(self, lookup):
        """
        Like `deconstruct`, but with a `wagtail.blocks.definition_lookup.BlockDefinitionLookupBuilder`
        object available so that any block instances within the definition can be added to the lookup
        table to obtain an ID (potentially shared with other matching block definitions, thus reducing
        the overall definition size) to be used in place of the block. The resulting deconstructed form
        returned here can then be restored into a block object using `Block.construct_from_lookup`.
        """
        # In the base implementation, no substitutions happen, so we ignore the lookup and just call
        # deconstruct
        return self.deconstruct()

    @classmethod
    def coerce(cls, value):
        """
        Normalize a child-block specification into a ``Block`` instance.

        A ``Block`` instance is used as-is and a ``Block`` subclass is
        instantiated — the long-standing shorthands. A *reference* — a callable
        returning a block class (e.g. ``lambda: MyBlock``) or a dotted import
        path string (e.g. ``"myapp.blocks.MyBlock"``) — is wrapped in the
        internal lazy mechanism so it is resolved on demand; this is how forward
        and cyclic references are expressed without the caller touching that
        mechanism directly. Subclasses may override this to customise how they
        interpret child specifications.
        """
        if isinstance(value, Block):
            return value
        if isinstance(value, type):
            if not issubclass(value, Block):
                raise TypeError(
                    "Expected a Block subclass, got non-Block class %r." % (value,)
                )
            return value()
        if isinstance(value, str) or callable(value):
            return LazyBlock(value)
        raise TypeError(
            "Expected a Block instance, a Block subclass, or a block reference "
            "(a callable returning a block, or a dotted import path); got %r."
            % (value,)
        )

    def has_deferred_reference(self):
        return False

    def __eq__(self, other):
        """
        Implement equality on block objects so that two blocks with matching definitions are considered
        equal. Block objects are intended to be immutable with the exception of set_name() and any meta
        attributes identified in MUTABLE_META_ATTRIBUTES, so checking these along with the result of
        deconstruct (which captures the constructor arguments) is sufficient to identify (valid) differences.

        This was implemented as a workaround for a Django <1.9 bug and is quite possibly not used by Wagtail
        any more, but has been retained as it provides a sensible definition of equality (and there's no
        reason to break it).
        """

        if self is other:
            # Identity short-circuit; the structural comparison below would
            # recurse forever on a cyclic block graph (e.g. a block that
            # resolves back to an enclosing block).
            return True

        if not isinstance(other, Block):
            # if the other object isn't a block at all, it clearly isn't equal.
            return False

            # Note that we do not require the two blocks to be of the exact same class. This is because
            # we may wish the following blocks to be considered equal:
            #
            # class FooBlock(StructBlock):
            #     first_name = CharBlock()
            #     surname = CharBlock()
            #
            # class BarBlock(StructBlock):
            #     first_name = CharBlock()
            #     surname = CharBlock()
            #
            # FooBlock() == BarBlock() == StructBlock([('first_name', CharBlock()), ('surname': CharBlock())])
            #
            # For this to work, StructBlock will need to ensure that 'deconstruct' returns the same signature
            # in all of these cases, including reporting StructBlock as the path:
            #
            # FooBlock().deconstruct() == (
            #     'wagtail.blocks.StructBlock',
            #     [('first_name', CharBlock()), ('surname': CharBlock())],
            #     {}
            # )
            #
            # This has the bonus side effect that the StructBlock field definition gets frozen into
            # the migration, rather than leaving the migration vulnerable to future changes to FooBlock / BarBlock
            # in models.py.

        return (
            self.name == other.name
            and self.deconstruct() == other.deconstruct()
            and all(
                getattr(self.meta, attr, None) == getattr(other.meta, attr, None)
                for attr in self.MUTABLE_META_ATTRIBUTES
            )
        )


class BoundBlock:
    def __init__(self, block, value, prefix=None, errors=None):
        self.block = block
        self.value = value
        self.prefix = prefix
        self.errors = errors

    def render(self, context=None):
        return self.block.render(self.value, context=context)

    def render_as_block(self, context=None):
        """
        Alias for render; the include_block tag will specifically check for the presence of a method
        with this name. (This is because {% include_block %} is just as likely to be invoked on a bare
        value as a BoundBlock. If we looked for a `render` method instead, we'd run the risk of finding
        an unrelated method that just happened to have that name - for example, when called on a
        PageChooserBlock it could end up calling page.render.
        """
        return self.block.render(self.value, context=context)

    def id_for_label(self):
        return self.block.id_for_label(self.prefix)

    def __str__(self):
        """Render the value according to the block's native rendering"""
        return self.block.render(self.value)

    def __repr__(self):
        return "<block {}: {!r}>".format(
            self.block.name or type(self.block).__name__,
            self.value,
        )


class DeclarativeSubBlocksMetaclass(BaseBlock):
    """
    Metaclass that collects sub-blocks declared on the base classes.
    (cheerfully stolen from https://github.com/django/django/blob/main/django/forms/forms.py)
    """

    def __new__(mcs, name, bases, attrs):
        # Collect sub-blocks declared on the current class.
        # These are available on the class as `declared_blocks`
        current_blocks = []
        for key, value in list(attrs.items()):
            if isinstance(value, Block):
                current_blocks.append((key, value))
                value.set_name(key)
                attrs.pop(key)
        current_blocks.sort(key=lambda x: x[1].creation_counter)
        attrs["declared_blocks"] = collections.OrderedDict(current_blocks)

        new_class = super().__new__(mcs, name, bases, attrs)

        # Walk through the MRO, collecting all inherited sub-blocks, to make
        # the combined `base_blocks`.
        base_blocks = collections.OrderedDict()
        for base in reversed(new_class.__mro__):
            # Collect sub-blocks from base class.
            if hasattr(base, "declared_blocks"):
                base_blocks.update(base.declared_blocks)

            # Field shadowing.
            for attr, value in base.__dict__.items():
                if value is None and attr in base_blocks:
                    base_blocks.pop(attr)
        new_class.base_blocks = base_blocks

        return new_class


# The chain of LazyBlock references currently being walked by a definition-graph
# traversal (check / defer / restore). A reference that resolves back to one
# already in the chain has closed a cycle, so the walk stops there. This lives
# in a ContextVar rather than as instance / class state on LazyBlock, because
# that would leak one traversal into another and recreate the class-attribute
# problem that broke cyclic lookups.
_active_lazyblock_walk = contextvars.ContextVar("wagtail_lazyblock_walk", default=())


class LazyBlock(Block):
    """
    A reference to another block definition that is resolved on demand. It lets
    a block graph contain cycles that Python's class definition order would
    otherwise forbid, such as a block that contains a list of itself::

        class CommentBlock(StructBlock):
            text = RichTextBlock()
            replies = ListBlock(lambda: CommentBlock)

    Container blocks (``ListBlock``, ``StreamBlock``, ``StructBlock``,
    ``TypedTableBlock``) accept a ``LazyBlock`` wherever a child block is
    expected, and also accept a callable (e.g. ``lambda: MyBlock``) or a dotted
    import path string (e.g. ``"myapp.blocks.MyBlock"``) which they wrap in a
    ``LazyBlock`` automatically via :meth:`Block.coerce`.

    The ``target`` may be:

    * a callable returning a block class, for example ``lambda: CommentBlock``.
    * a dotted import path, for example ``"myapp.blocks.AccordionBlock"``.
    * a block class directly.

    Any further positional and keyword arguments are passed to the target's
    constructor when it is resolved (so ``LazyBlock(lambda: MyBlock, required=False)``
    works), and they form part of the reference's identity when detecting cycles.

    All cycle handling lives on this class. The definition-graph traversals
    (``check``, ``defer_required_validation``, ``restore_deferred_validation``)
    are guarded so that a cycle is broken in a single place; value methods
    delegate to the resolved target.

    A cycle must pass through a block that holds zero or more child items added
    on demand (``ListBlock``, ``StreamBlock``, ``TypedTableBlock``). Routing a
    cycle only through always-rendered blocks (such as ``StructBlock`` referring
    back to itself) is **not supported**: the editor cannot render it (it would
    expand without end) and building a default recurses without limit. This is a
    documented constraint, not a validated one — such a definition will fail at
    runtime rather than with a tidy error.
    """

    def __init__(self, target, *args, **kwargs):
        # The standard Block setup (meta, creation counter, registry, …) with no
        # meta options: **kwargs are for the target constructor, not Block meta, so
        # they are deliberately not forwarded to super().__init__().
        super().__init__()
        self.target = target
        self.target_args = args
        self.target_kwargs = kwargs
        self.resolved_block = None
        # Set by construct_from_lookup for index-based migration reconstruction.
        # When set, resolve() fetches the block from the lookup table instead of
        # instantiating get_target_class().
        self.lazy_lookup = None
        self.lazy_lookup_index = None

    def resolve(self):
        """Return the target block instance, instantiating it once and caching it."""
        if self.resolved_block is None:
            if self.lazy_lookup is not None:
                self.resolved_block = self.lazy_lookup.get_block(self.lazy_lookup_index)
            else:
                self.resolved_block = self._build_target()
            if self.name:
                self.resolved_block.set_name(self.name)
        return self.resolved_block

    def _build_target(self):
        """Instantiate the target block for an ordinary (non-lookup) reference."""
        target = self.target
        if isinstance(target, str):
            module_name, class_name = target.rsplit(".", 1)
            target = getattr(import_module(module_name), class_name)
        if isinstance(target, type):
            return target(*self.target_args, **self.target_kwargs)
        if callable(target):
            result = target()
            # A callable returning a ready-made block instance is used as-is, so its
            # own constructor arguments are preserved; only a returned class is
            # instantiated with this LazyBlock's extra args.
            if isinstance(result, Block):
                return result
            if isinstance(result, type):
                return result(*self.target_args, **self.target_kwargs)
        raise ImproperlyConfigured(
            "LazyBlock target must be a dotted import path, a block class, or a "
            "callable returning one; got %r." % (self.target,)
        )

    def get_target_class(self):
        """Resolve ``self.target`` to a block class without instantiating it."""
        if self.lazy_lookup is not None:
            # Reconstructed from a lookup table: the placeholder callable is never the
            # real target, so derive the class from the block fetched from the lookup.
            return type(self.resolve())
        target = self.target
        if isinstance(target, str):
            module_name, class_name = target.rsplit(".", 1)
            return getattr(import_module(module_name), class_name)
        if isinstance(target, type):
            return target
        if callable(target):
            result = target()
            return result if isinstance(result, type) else type(result)
        raise ImproperlyConfigured(
            "LazyBlock target must be a dotted import path, a block class, or a "
            "callable returning one; got %r." % (target,)
        )

    @property
    def cycle_key(self):
        """
        Return the identity used for cycle detection and lookup serialization.

        Blocks reconstructed from a lookup must key off the lookup object and
        index they came from, so they round-trip back to that same definition.
        Ordinary lazy references key off the resolved target class and the
        constructor arguments that define the reference.
        """
        if self.lazy_lookup_index is not None:
            # Identity for a reference reconstructed from a lookup table: the lookup
            # object plus the index it was read from, so it round-trips back to the
            # same definition. Compared only for equality, never hashed; the lookup
            # object has no __eq__ so it compares by identity.
            return (
                self.lazy_lookup,
                self.lazy_lookup_index,
                self.target_args,
                self.target_kwargs,
            )

        target_class = self.get_target_class()
        path = "%s.%s" % (target_class.__module__, target_class.__qualname__)
        return (path, self.target_args, self.target_kwargs)

    @contextmanager
    def walking(self):
        """
        Guard for definition-graph traversals.

        Yield ``False`` when this traversal has come back to the same lazy
        reference, so the caller can stop at the cycle edge. Otherwise yield
        ``True`` and add this reference to the active walk for the duration of
        the context manager.
        """
        key = self.cycle_key
        if any(active == key for active in _active_lazyblock_walk.get()):
            yield False
            return

        token = _active_lazyblock_walk.set(_active_lazyblock_walk.get() + (key,))
        try:
            yield True
        finally:
            _active_lazyblock_walk.reset(token)

    def check(self, **kwargs):
        errors = []
        with self.walking() as entered:
            if not entered:
                return errors
            errors.extend(self.resolve().check(**kwargs))
        return errors

    def defer_required_validation(self):
        with self.walking() as entered:
            if entered:
                self.resolve().defer_required_validation()

    def restore_deferred_validation(self):
        with self.walking() as entered:
            if entered:
                self.resolve().restore_deferred_validation()

    def has_deferred_reference(self):
        return True

    def deconstruct(self):
        # Serialize the reference itself as a dotted path. Index-based references
        # are only for lookup-aware deconstruction.
        target_class = self.get_target_class()
        path = self.canonical_module_path
        args = (
            "%s.%s" % (target_class.__module__, target_class.__qualname__),
            *self.target_args,
        )
        kwargs = self.target_kwargs
        return (path, args, kwargs)

    def deconstruct_with_lookup(self, lookup):
        path = self.canonical_module_path
        # Reserve by explicit identity so a cyclic graph can point back to the
        # same in-progress definition, while ordinary blocks still deduplicate
        # structurally in the lookup builder.
        index = lookup.add_block(self.resolve(), identity=self.cycle_key)
        args = [index, *self.target_args]
        kwargs = self.target_kwargs
        return (path, args, kwargs)

    @classmethod
    def construct_from_lookup(cls, lookup, index, *target_args, **target_kwargs):
        # The placeholder target is never called. Once lookup/index are set,
        # resolve() reconstructs the target block from the lookup table instead.
        lazy = cls(lambda: None, *target_args, **target_kwargs)
        lazy.lazy_lookup = lookup
        lazy.lazy_lookup_index = index
        return lazy

    def to_python(self, *args, **kwargs):
        return self.resolve().to_python(*args, **kwargs)

    def bulk_to_python(self, values):
        # Short-circuit the empty case so we don't resolve the target at all: for a
        # self-referential reference, resolving on every empty call would recurse
        # forever. With data, delegate as normal.
        if not values:
            return []
        return self.resolve().bulk_to_python(values)

    def value_from_datadict(self, *args, **kwargs):
        return self.resolve().value_from_datadict(*args, **kwargs)

    def value_omitted_from_data(self, *args, **kwargs):
        return self.resolve().value_omitted_from_data(*args, **kwargs)

    def bind(self, *args, **kwargs):
        return self.resolve().bind(*args, **kwargs)

    def clean(self, *args, **kwargs):
        return self.resolve().clean(*args, **kwargs)

    def clean_deferred(self, *args, **kwargs):
        return self.resolve().clean_deferred(*args, **kwargs)

    def normalize(self, *args, **kwargs):
        return self.resolve().normalize(*args, **kwargs)

    def get_default(self, *args, **kwargs):
        return self.resolve().get_default(*args, **kwargs)

    def get_prep_value(self, *args, **kwargs):
        return self.resolve().get_prep_value(*args, **kwargs)

    def get_api_representation(self, *args, **kwargs):
        return self.resolve().get_api_representation(*args, **kwargs)

    def get_form_state(self, *args, **kwargs):
        return self.resolve().get_form_state(*args, **kwargs)

    def get_context(self, *args, **kwargs):
        return self.resolve().get_context(*args, **kwargs)

    def get_template(self, *args, **kwargs):
        return self.resolve().get_template(*args, **kwargs)

    def render(self, *args, **kwargs):
        return self.resolve().render(*args, **kwargs)

    def render_basic(self, *args, **kwargs):
        return self.resolve().render_basic(*args, **kwargs)

    def get_searchable_content(self, *args, **kwargs):
        return self.resolve().get_searchable_content(*args, **kwargs)

    def extract_references(self, *args, **kwargs):
        return self.resolve().extract_references(*args, **kwargs)

    def get_block_by_content_path(self, *args, **kwargs):
        return self.resolve().get_block_by_content_path(*args, **kwargs)

    def get_description(self, *args, **kwargs):
        return self.resolve().get_description(*args, **kwargs)

    def get_preview_template(self, *args, **kwargs):
        return self.resolve().get_preview_template(*args, **kwargs)

    def get_preview_context(self, *args, **kwargs):
        return self.resolve().get_preview_context(*args, **kwargs)

    def get_preview_value(self, *args, **kwargs):
        return self.resolve().get_preview_value(*args, **kwargs)

    def id_for_label(self, *args, **kwargs):
        return self.resolve().id_for_label(*args, **kwargs)


class LazyBlockAdapter(Adapter):
    def build_node(self, obj, context):
        return context.build_node(obj.resolve())


register_telepath_adapter(LazyBlockAdapter(), LazyBlock)


# ========================
# django.forms integration
# ========================


@register_telepath_adapter
class BlockWidget(forms.Widget):
    """Wraps a block object as a widget so that it can be incorporated into a Django form"""

    def __init__(self, block_def, attrs=None):
        super().__init__(attrs=attrs)
        self.block_def = block_def
        self._js_context = None
        self._block_json = None

    def _build_block_json(self):
        try:
            self._js_context = JSContext()
            self._block_json = json.dumps(self._js_context.pack(self.block_def))
        except Exception as e:  # noqa: BLE001
            raise ValueError("Error while serializing block definition: %s" % e) from e

    @property
    def js_context(self):
        if self._js_context is None:
            self._build_block_json()

        return self._js_context

    @property
    def block_json(self):
        if self._block_json is None:
            self._build_block_json()

        return self._block_json

    def id_for_label(self, prefix):
        # Delegate the job of choosing a label ID to the top-level block.
        # (In practice, the top-level block will typically be a StreamBlock, which returns None.)
        return self.block_def.id_for_label(prefix)

    def render_with_errors(self, name, value, attrs=None, errors=None, renderer=None):
        value_json = json.dumps(self.block_def.get_form_state(value))

        if errors:
            # errors is expected to be an ErrorList consisting of a single validation error
            error = errors.as_data()[0]
            error_json = json.dumps(get_error_json_data(error))
        else:
            error_json = json.dumps(None)

        return format_html(
            """
                <div id="{id}" data-block data-controller="w-block" data-w-block-data-value="{block_json}" data-w-block-arguments-value="[{value_json},{error_json}]"></div>
            """,
            id=name,
            block_json=self.block_json,
            value_json=value_json,
            error_json=error_json,
        )

    def render(self, name, value, attrs=None, renderer=None):
        return self.render_with_errors(
            name, value, attrs=attrs, errors=None, renderer=renderer
        )

    @cached_property
    def media(self):
        return self.js_context.media + forms.Media(
            js=[
                # this will almost certainly be
                # pulled in by the block adapters too
                versioned_static("wagtailadmin/js/telepath/blocks.js"),
            ],
            css={
                "all": [
                    versioned_static("wagtailadmin/css/panels/streamfield.css"),
                ]
            },
        )

    def value_from_datadict(self, data, files, name):
        return self.block_def.value_from_datadict(data, files, name)

    def value_omitted_from_data(self, data, files, name):
        return self.block_def.value_omitted_from_data(data, files, name)

    def telepath_pack(self, context):
        return ("wagtail.widgets.BlockWidget", [])


class BlockField(forms.Field):
    """Wraps a block object as a form field so that it can be incorporated into a Django form"""

    def __init__(self, block=None, **kwargs):
        if block is None:
            raise ImproperlyConfigured("BlockField was not passed a 'block' object")
        self.block = block

        if "widget" not in kwargs:
            kwargs["widget"] = BlockWidget(block)

        super().__init__(**kwargs)

    def clean(self, value):
        # During deferred validation, form fields (including BlockField) have an
        # is_deferred_validation attribute set to True. Use this to determine
        # whether to call the block's clean_deferred method (which will perform any
        # necessary setup/teardown for deferred validation, and then call clean)
        # or to call clean directly.
        is_deferred_validation = getattr(self, "is_deferred_validation", False)
        if is_deferred_validation:
            return self.block.clean_deferred(value)
        else:
            return self.block.clean(value)

    def has_changed(self, initial_value, data_value):
        return self.block.get_prep_value(initial_value) != self.block.get_prep_value(
            data_value
        )


@lru_cache(maxsize=None)
def get_help_icon():
    return render_to_string(
        "wagtailadmin/shared/icon.html", {"name": "help", "classname": "default"}
    )


def get_error_json_data(error):
    """
    Translate a ValidationError instance raised against a block (which may potentially be a
    ValidationError subclass specialised for a particular block type) into a JSON-serialisable dict
    consisting of one or both of:
    messages: a list of error message strings to be displayed against the block
    blockErrors: a structure specific to the block type, containing further error objects in this
        format to be displayed against this block's children
    """
    if hasattr(error, "as_json_data"):
        return error.as_json_data()
    else:
        return {"messages": error.messages}


def get_error_list_json_data(error_list):
    """
    Flatten an ErrorList instance containing any number of ValidationErrors
    (which may themselves contain multiple messages) into a list of error message strings.
    This does not consider any other properties of ValidationError other than `message`,
    so should not be used where ValidationError subclasses with nested block errors may be
    present.
    (In terms of StreamBlockValidationError et al: it's valid for use on non_block_errors
    but not block_errors)
    """
    return list(itertools.chain(*(err.messages for err in error_list.as_data())))


DECONSTRUCT_ALIASES = {
    Block: "wagtail.blocks.Block",
    LazyBlock: "wagtail.blocks.LazyBlock",
}
