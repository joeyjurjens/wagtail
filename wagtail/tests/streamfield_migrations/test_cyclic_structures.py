from django.test import TestCase

from wagtail.blocks.migrations.operations import (
    RemoveStructChildrenOperation,
    RenameStructChildrenOperation,
)
from wagtail.blocks.migrations.utils import apply_changes_to_raw_data
from wagtail.test.streamfield_migrations import models


class SelfRefStructBlockMigrationTest(TestCase):
    """
    Tests that block migration utilities handle a self-referential cyclic block
    (SelfRefStructBlock.selfref is a ListBlock of LazyBlock(lambda: SelfRefStructBlock)).
    """

    def setUp(self):
        self.raw_data = [
            {
                "type": "selfrefstruct",
                "id": "a",
                "value": {
                    "char1": "Parent",
                    "selfref": [
                        {
                            "type": "item",
                            "id": "r1",
                            "value": {"char1": "Child 1", "selfref": []},
                        },
                        {
                            "type": "item",
                            "id": "r2",
                            "value": {"char1": "Child 2", "selfref": []},
                        },
                    ],
                },
            },
            {"type": "char1", "id": "b", "value": "Other"},
        ]

    def test_blocks_and_data_not_operated_on_intact(self):
        altered = apply_changes_to_raw_data(
            raw_data=self.raw_data,
            block_path_str="selfrefstruct.selfref.item",
            operation=RenameStructChildrenOperation(
                old_name="char1", new_name="renamed1"
            ),
            streamfield=models.SampleModel.content,
        )
        # Non-selfrefstruct block unchanged
        self.assertEqual(altered[1], self.raw_data[1])
        # Top-level char1 unchanged
        self.assertEqual(altered[0]["value"]["char1"], "Parent")
        self.assertEqual(altered[0]["id"], "a")

    def test_rename_in_nested_child(self):
        altered = apply_changes_to_raw_data(
            raw_data=self.raw_data,
            block_path_str="selfrefstruct.selfref.item",
            operation=RenameStructChildrenOperation(
                old_name="char1", new_name="renamed1"
            ),
            streamfield=models.SampleModel.content,
        )
        # Both children renamed
        self.assertEqual(
            altered[0]["value"]["selfref"][0]["value"]["renamed1"], "Child 1"
        )
        self.assertEqual(
            altered[0]["value"]["selfref"][1]["value"]["renamed1"], "Child 2"
        )
        self.assertNotIn("char1", altered[0]["value"]["selfref"][0]["value"])
        # IDs preserved
        self.assertEqual(altered[0]["value"]["selfref"][0]["id"], "r1")
        self.assertEqual(altered[0]["value"]["selfref"][1]["id"], "r2")

    def test_remove_in_nested_child(self):
        altered = apply_changes_to_raw_data(
            raw_data=self.raw_data,
            block_path_str="selfrefstruct.selfref.item",
            operation=RemoveStructChildrenOperation(name="char1"),
            streamfield=models.SampleModel.content,
        )
        self.assertNotIn("char1", altered[0]["value"]["selfref"][0]["value"])
        self.assertIn("selfref", altered[0]["value"]["selfref"][0]["value"])

    def test_rename_at_two_levels_deep(self):
        raw_data = [
            {
                "type": "selfrefstruct",
                "id": "a",
                "value": {
                    "char1": "Root",
                    "selfref": [
                        {
                            "type": "item",
                            "id": "r1",
                            "value": {
                                "char1": "Level 1",
                                "selfref": [
                                    {
                                        "type": "item",
                                        "id": "r2",
                                        "value": {"char1": "Level 2", "selfref": []},
                                    },
                                ],
                            },
                        }
                    ],
                },
            },
        ]
        altered = apply_changes_to_raw_data(
            raw_data=raw_data,
            block_path_str="selfrefstruct.selfref.item.selfref.item",
            operation=RenameStructChildrenOperation(
                old_name="char1", new_name="renamed1"
            ),
            streamfield=models.SampleModel.content,
        )
        level2 = altered[0]["value"]["selfref"][0]["value"]["selfref"][0]["value"]
        self.assertEqual(level2["renamed1"], "Level 2")
        self.assertNotIn("char1", level2)
        # Non-targeted levels intact
        self.assertEqual(altered[0]["value"]["char1"], "Root")
        self.assertEqual(altered[0]["value"]["selfref"][0]["value"]["char1"], "Level 1")
        self.assertEqual(altered[0]["value"]["selfref"][0]["id"], "r1")


class MutualStructBlockMigrationTest(TestCase):
    """
    Tests that block migration utilities handle a mutually-referential cyclic block
    (MutualStructBlock ↔ MutualStreamBlock via LazyBlock).
    """

    def setUp(self):
        self.raw_data = [
            {
                "type": "mutualstruct",
                "id": "a",
                "value": {
                    "char1": "Outer",
                    "stream1": [
                        {
                            "type": "struct1",
                            "id": "n1",
                            "value": {"char1": "Inner 1", "stream1": []},
                        },
                        {
                            "type": "struct1",
                            "id": "n2",
                            "value": {"char1": "Inner 2", "stream1": []},
                        },
                        {"type": "char1", "id": "p1", "value": "Some text"},
                    ],
                },
            },
            {"type": "char1", "id": "b", "value": "Other"},
        ]

    def test_blocks_and_data_not_operated_on_intact(self):
        altered = apply_changes_to_raw_data(
            raw_data=self.raw_data,
            block_path_str="mutualstruct.stream1.struct1",
            operation=RenameStructChildrenOperation(
                old_name="char1", new_name="renamed1"
            ),
            streamfield=models.SampleModel.content,
        )
        # Non-mutualstruct block unchanged
        self.assertEqual(altered[1], self.raw_data[1])
        # Outer char1 unchanged
        self.assertEqual(altered[0]["value"]["char1"], "Outer")
        self.assertEqual(altered[0]["id"], "a")
        # Non-struct1 child unchanged
        self.assertEqual(
            altered[0]["value"]["stream1"][2], self.raw_data[0]["value"]["stream1"][2]
        )

    def test_rename_in_nested_struct(self):
        altered = apply_changes_to_raw_data(
            raw_data=self.raw_data,
            block_path_str="mutualstruct.stream1.struct1",
            operation=RenameStructChildrenOperation(
                old_name="char1", new_name="renamed1"
            ),
            streamfield=models.SampleModel.content,
        )
        self.assertEqual(
            altered[0]["value"]["stream1"][0]["value"]["renamed1"], "Inner 1"
        )
        self.assertEqual(
            altered[0]["value"]["stream1"][1]["value"]["renamed1"], "Inner 2"
        )
        self.assertNotIn("char1", altered[0]["value"]["stream1"][0]["value"])
        # IDs preserved
        self.assertEqual(altered[0]["value"]["stream1"][0]["id"], "n1")
        self.assertEqual(altered[0]["value"]["stream1"][1]["id"], "n2")

    def test_remove_in_nested_struct(self):
        altered = apply_changes_to_raw_data(
            raw_data=self.raw_data,
            block_path_str="mutualstruct.stream1.struct1",
            operation=RemoveStructChildrenOperation(name="char1"),
            streamfield=models.SampleModel.content,
        )
        self.assertNotIn("char1", altered[0]["value"]["stream1"][0]["value"])
        self.assertIn("stream1", altered[0]["value"]["stream1"][0]["value"])
