from django.db import models

from wagtail.blocks import (
    BlockReference,
    CharBlock,
    ListBlock,
    StreamBlock,
    StructBlock,
)
from wagtail.fields import StreamField
from wagtail.models import Page


class SimpleStructBlock(StructBlock):
    char1 = CharBlock()
    char2 = CharBlock()


class SimpleStreamBlock(StreamBlock):
    char1 = CharBlock()
    char2 = CharBlock()


class NestedStructBlock(StructBlock):
    char1 = CharBlock()
    stream1 = SimpleStreamBlock()
    struct1 = SimpleStructBlock()
    list1 = ListBlock(CharBlock())


class NestedStreamBlock(StreamBlock):
    char1 = CharBlock()
    stream1 = SimpleStreamBlock()
    struct1 = SimpleStructBlock()
    list1 = ListBlock(CharBlock())


class SelfRefStructBlock(StructBlock):
    char1 = CharBlock()
    selfref = ListBlock(BlockReference(lambda: SelfRefStructBlock))


class MutualStructBlock(StructBlock):
    char1 = CharBlock()
    stream1 = BlockReference(lambda: MutualStreamBlock)


class MutualStreamBlock(StreamBlock):
    struct1 = MutualStructBlock()
    char1 = CharBlock()


class BaseStreamBlock(StreamBlock):
    char1 = CharBlock()
    char2 = CharBlock()
    simplestruct = SimpleStructBlock()
    simplestream = SimpleStreamBlock()
    simplelist = ListBlock(CharBlock())
    nestedstruct = NestedStructBlock()
    nestedstream = NestedStreamBlock()
    nestedlist_struct = ListBlock(SimpleStructBlock())
    nestedlist_stream = ListBlock(SimpleStreamBlock())
    selfrefstruct = SelfRefStructBlock()
    mutualstruct = MutualStructBlock()


class SampleModel(models.Model):
    content = StreamField(BaseStreamBlock())


class SamplePage(Page):
    content = StreamField(BaseStreamBlock())
