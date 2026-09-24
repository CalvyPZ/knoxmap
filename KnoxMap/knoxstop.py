"""Stopping a map that is being made, without losing the area it is of.

Generating, building and compiling a town all take minutes, and until now the
only way out of one was to close the window - which threw away the rectangle
drawn on the map with it, so the area had to be found and drawn again.

The window asks the server to stop (POST /api/stop); the long jobs are handed
a `should_stop` callable and look at it wherever they can be interrupted
without leaving half a file behind - between Overpass tiles, between
buildings, between compile batches. When it says yes they raise Stopped,
which the app turns into an ordinary "stopped" reply rather than an error.

Nothing here touches the drawn area or the settings: a stopped map is simply
one that has not been made yet, and pressing the button again starts it over.
"""
from __future__ import annotations


class Stopped(Exception):
    """Raised inside a long job when the window has asked it to stop."""

    def __init__(self, what: str = "the job"):
        super().__init__(f"Stopped {what}.")
        self.what = what


def check(should_stop, what: str = "the job") -> None:
    """Raise Stopped if the window has asked for it. Cheap enough to call in
    a loop: `should_stop` is a set lookup."""
    if should_stop is not None and should_stop():
        raise Stopped(what)
