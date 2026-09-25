"""
URL routing for smart.park.

Classes:
  Blueprint  : groups related routes under one name (used in every controller).
  Router     : central dispatcher — matches incoming paths to handler functions.
  Route      : internal container for one registered route.

Path parameters use angle-bracket syntax: /admin/slots/<slot_id>
The captured value is available in the controller via request.param("slot_id").
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from core.http import Request

_PARAM_PATTERN = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")
# This regular expression is used to find dynamic parameters inside a URL path
# - [a-zA-Z_] as the first character, which must be a letter or underscore
# - [a-zA-Z0-9_]* as the remaining characters, which may contain
#   letters, numbers, or underscores.


def _compile_path(path: str) -> re.Pattern:
    """Turn a path pattern ("/admin/slots/<slot_id>") into a compiled
    regex with one named group per <param>."""
    # Search the path for every parameter that follows the <parameter>
    # format.
    # _PARAM_PATTERN.sub() replaces each parameter that it finds.
    # The lambda function receives the matched parameter as "m".
    # m.group(1) retrieves only the parameter name without < and >.
    # (?P<name>[^/]+) creates a named regular-expression group.
    # This means:
    # - ?P<name> gives the captured value a name.
    # - [^/]+ means one or more characters that are not "/".
    pattern = _PARAM_PATTERN.sub(lambda m: f"(?P<{m.group(1)}>[^/]+)", path)
    return re.compile(f"^{pattern}$")
 # re.compile() converts the final regular-expression string into a
    # compiled regex object that can efficiently be used for route matching.


class Route:
    __slots__ = ("methods", "pattern", "handler", "endpoint")

    # __slots__ defines the exact attributes that objects of this class
    # are allowed to have

    def __init__(self, methods: set[str], pattern: re.Pattern, handler: Callable, endpoint: str):
        self.methods = methods
        # Store the HTTP methods allowed for this route.
        self.pattern = pattern
        # Store the compiled regular-expression pattern used to determine
        # whether an incoming URL matches this route.
        self.handler = handler
        # Store the Python function that should execute when this route
        # is successfully matched.
        self.endpoint = endpoint
        # Store the unique name used to identify this route.
        # The endpoint is later used by url_for() to build URLs.


class Blueprint:
    """
    Groups related routes under one name and an optional URL prefix.

    Usage:

        guard_bp = Blueprint("guard")

        @guard_bp.route("/dashboard")
        def dashboard(req: Request) -> Response:
            ...
    """

    def __init__(self, name: str):
        self.name = name
        # Store the name of the Blueprint.
        self.routes: list[tuple[set[str], str, Callable, str]] = []
        # Create an empty list that will store the Blueprint's routes.

    def route(self, path: str, methods: Optional[list[str]] = None):

        methods_set = {m.upper() for m in (methods or ["GET"])}

        # A set is used because it provides a convenient collection of
        # allowed methods without duplicate values.

        def decorator(func: Callable) -> Callable:
            # Create a unique endpoint name by combining the Blueprint
            # name with the Python function name.
            # This allows routes to be identified by a name rather than
            # only by their URL.
            endpoint = f"{self.name}.{func.__name__}"
            self.routes.append((methods_set, path, func, endpoint))
            # Save the route information inside the Blueprint.
            return func

        return decorator
    # Python will use this returned function when the route decorator
    # is placed above a handler function.


class Router:
    """
    The central URL dispatcher. main.py builds one Router, registers
    every Blueprint onto it, then calls dispatch() for each request.
    """

    def __init__(self):
        self._routes: list[Route] = []
        # Create an empty list that will contain all registered Route
        # objects.
        # The Router searches this list whenever it needs to find the
        # correct handler for an incoming request.
        self._endpoints: dict[str, str] = {}
        # Create a dictionary that maps endpoint names to their original
        # URL path templates.

    def add_route(self, methods: set[str], path: str, handler: Callable, endpoint: str) -> None:
        # Convert the URL path into a compiled regular expression.
        # This allows the Router to match both fixed parts of the URL
        # and dynamic parameters.
        self._routes.append(Route(methods, _compile_path(path), handler, endpoint))
        self._endpoints[endpoint] = path
        # Store the endpoint and its original path template.
        # This is later used by url_for() when another part of the
        # application needs to generate the URL for this route.

    def register_blueprint(self, blueprint: Blueprint, url_prefix: str = "") -> None:
        for methods, path, handler, endpoint in blueprint.routes:
            # Go through every route that has been registered inside the
            # Blueprint.
            full_path = f"{url_prefix.rstrip('/')}{path}" if url_prefix else path
            # Add the optional URL prefix to the route.
            # rstrip("/") removes trailing "/" characters from the prefix
            # so that the final URL does not accidentally contain
            # unnecessary duplicate slashes.
            # If no prefix was supplied, the original path is used.
            self.add_route(methods, full_path, handler, endpoint)
            # Add the completed route to the central Router.
            # After this call, the Router can match incoming requests
            # against this Blueprint route.

    def url_for(self, endpoint: str, **values) -> str:
        """
        Build a URL from an endpoint name and path parameters.
        Extra keyword arguments become query string parameters.

        Special case: url_for("static", filename="...") resolves to
        "/static/<filename>" — served by main.py's _serve_static().
        """
        if endpoint == "static":
            # Handle the special "static" endpoint separately.
            # Static files are served using /static/<filename>, so this does
            # not need to be looked up in the normal route dictionary.
            filename = values.get("filename", "")
            # Get the filename supplied to url_for().
            # If no filename was supplied, use an empty string
            return f"/static/{filename}"
            # Build and return the static-file URL.

        template = self._endpoints.get(endpoint)
        # Look up the URL template belonging to the requested endpoint.
        # .get() returns None if the endpoint does not exist.
        if template is None:
            raise ValueError(f"Unknown endpoint: {endpoint}")
        # If the endpoint was not registered, stop execution and raise
        # an error because the application cannot build its URL.

        path = template
        # Start with the original URL template
        # This variable will be changed as dynamic parameters are
        # replaced with their actual values.
        used_keys = set()
        # Keep track of which values were used to replace parameters
        # in the URL path.# This is necessary because unused values may need to become
        # query-string parameters.
        for match in _PARAM_PATTERN.finditer(template):
            # Find every dynamic parameter in the original URL template.
            name = match.group(1)
            # Get the parameter name from the regex match.
            if name in values:
                # Check whether the caller supplied a value for this parameter.
                path = path.replace(f"<{name}>", str(values[name]))
                # Replace the <parameter> placeholder with the actual
                # value supplied by the caller.
                used_keys.add(name)
                # Record that this value has already been used in the
                # URL path.

        extra = {k: v for k, v in values.items() if k not in used_keys}
        # Find all supplied values that were not used as path parameters.
        # These extra values will be converted into URL query parameters.
        if extra:
            from urllib.parse import urlencode
            # Import urlencode to safely convert the dictionary of
            # extra values into URL query-string format.
            path = f"{path}?{urlencode(extra)}"
            # Add the encoded query parameters to the URL.
        return path

    def dispatch(self, request: Request) -> tuple[Optional[Route], dict]:
        """
        Find the route matching this request's path and method.

        Returns (route, path_params) on success, or (None, {}) if no
        route matches at all (404), or raises MethodNotAllowed if the
        path matches some route but not with this HTTP method (405).
        """
        path_matched = False
        # This variable records whether the request path matched at least
        # one route  # It is initially False because no routes have been checked yet.
        for route in self._routes:
            # Check the incoming request against every registered route.
            match = route.pattern.match(request.path)
            # Try to match the request path against this route's compiled
            # regular-expression pattern.
            if not match:
                continue
            # If the URL does not match this route, move to the next route.
            path_matched = True
            if request.method in route.methods:
                # Check whether the HTTP method used by the request is allowed
                # for this route.
                return route, match.groupdict()
            # match.groupdict() returns the values captured from
            # dynamic URL parameters as a dictionary.
            # Return both the matched Route and its path parameters.
        if path_matched:
            raise MethodNotAllowed()
        # If the path matched a route but none of the allowed HTTP methods
        # matched the request method, the problem is the HTTP method rather  # than the URL.
        # Raise MethodNotAllowed so the application can respond with # HTTP 405.
        return None, {}


class MethodNotAllowed(Exception):
    """Raised when a path matches a route but not the HTTP method used."""
