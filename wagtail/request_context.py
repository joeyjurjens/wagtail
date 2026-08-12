from contextlib import contextmanager

from asgiref.local import Local

from wagtail.models import Site

_request_storage = Local()


@contextmanager
def with_request(request):
    """
    A context manager that stores the request in thread-local storage for the
    duration of the context. This allows code that doesn't have access to the
    request to retrieve it from get_current_request(). Initially made for the
    richtext LinkHandler, as the .url could be inaccurate without the request.
    """
    try:
        _request_storage.request = request
        _request_storage.site = Site.find_for_request(request)
        yield
    finally:
        del _request_storage.request
        del _request_storage.site


def get_current_request():
    """Returns the current request if available."""
    return getattr(_request_storage, "request", None)


def get_current_site():
    """Returns the current site if available."""
    return getattr(_request_storage, "site", None)
