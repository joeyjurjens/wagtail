import logging
from contextlib import contextmanager

from asgiref.local import Local
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import (
    post_delete,
    post_migrate,
    post_save,
    pre_delete,
    pre_migrate,
)

from wagtail.models import Locale, Page, ReferenceIndex, Site

from .tasks import update_reference_index_task

logger = logging.getLogger("wagtail")


# Clear the wagtail_site_root_paths from the cache whenever Site records are updated.
def post_save_site_signal_handler(instance, update_fields=None, **kwargs):
    Site.clear_site_root_paths_cache()


def post_delete_site_signal_handler(instance, **kwargs):
    Site.clear_site_root_paths_cache()


def pre_delete_page_unpublish(sender, instance, **kwargs):
    # Make sure pages are unpublished before deleting
    if instance.live:
        # Don't bother to save, this page is just about to be deleted!
        instance.unpublish(commit=False, log_action=None)


def post_delete_page_log_deletion(sender, instance, **kwargs):
    logger.info('Page deleted: "%s" id=%d', instance.title, instance.id)


def reset_locales_display_names_cache(sender, instance, **kwargs):
    cache.delete("wagtail_locales_display_name")


reference_index_auto_update_disabled = Local()


@contextmanager
def disable_reference_index_auto_update():
    """
    A context manager that can be used to temporarily disable the reference index auto-update signal handlers.

    For example:

    with disable_reference_index_auto_update():
        my_instance.save()  # Reference index will not be updated by this save
    """
    try:
        reference_index_auto_update_disabled.value = True
        yield
    finally:
        del reference_index_auto_update_disabled.value


def update_reference_index_on_save(instance, **kwargs):
    # Don't populate reference index while loading fixtures as referenced objects may not be populated yet
    if kwargs.get("raw", False):
        return

    if getattr(reference_index_auto_update_disabled, "value", False):
        return

    update_reference_index_task.enqueue(
        instance._meta.app_label, instance._meta.model_name, str(instance.pk)
    )


def remove_reference_index_on_delete(instance, **kwargs):
    if getattr(reference_index_auto_update_disabled, "value", False):
        return

    with transaction.atomic():
        ReferenceIndex.remove_for_object(instance)


def connect_reference_index_signal_handlers_for_model(model):
    post_save.connect(update_reference_index_on_save, sender=model)
    post_delete.connect(remove_reference_index_on_delete, sender=model)


def connect_reference_index_signal_handlers(**kwargs):
    for model in ReferenceIndex.tracked_models:
        connect_reference_index_signal_handlers_for_model(model)


def disconnect_reference_index_signal_handlers_for_model(model):
    post_save.disconnect(update_reference_index_on_save, sender=model)
    post_delete.disconnect(remove_reference_index_on_delete, sender=model)


def disconnect_reference_index_signal_handlers(**kwargs):
    for model in ReferenceIndex.tracked_models:
        disconnect_reference_index_signal_handlers_for_model(model)


def register_signal_handlers():
    post_save.connect(post_save_site_signal_handler, sender=Site)
    post_delete.connect(post_delete_site_signal_handler, sender=Site)

    pre_delete.connect(pre_delete_page_unpublish, sender=Page)
    post_delete.connect(post_delete_page_log_deletion, sender=Page)

    post_save.connect(reset_locales_display_names_cache, sender=Locale)
    post_delete.connect(reset_locales_display_names_cache, sender=Locale)

    # Disconnect reference index signals while migrations are running
    # (we don't want to log references in migrations as the ReferenceIndex model might not exist)
    pre_migrate.connect(disconnect_reference_index_signal_handlers)
    post_migrate.connect(connect_reference_index_signal_handlers)
