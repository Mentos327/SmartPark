"""
Application entry point for smart.park.

Starts the HTTP server, wires up all controllers, and initialises the database.

Run with:
    python main.py
"""
from __future__ import annotations

import mimetypes
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import Config
from core.http import Request, Response, StreamingResponse
from core.router import Router, MethodNotAllowed
from core.templating import build_environment, render as render_template
from database.db import init_db
from auth.session_manager import load_session_into_request, save_session_from_request
from auth import decorators as auth_decorators

# Creates the path to the application's static folder.
# __file__ represents the current Python file.
# Path(__file__) converts that file location into a Path object.
# .resolve() gets the absolute path.
# .parent gets the folder containing the current Python file.
# / "static" adds the static folder to that location.
# The final value is stored in STATIC_DIR.
STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_router() -> Router:
    """
    Build the Router and register every controller's Blueprint with the
    appropriate URL prefixes, then return the configured Router.
    """
    # Import the controller modules used by different types of users
    # and different parts of the SmartPark system.
    # Each controller contains routes and functionality for a specific
    # part of the application.
    from controllers import (
        auth as auth_controller, driver as driver_controller, admin as admin_controller,
        super_admin as super_admin_controller, guard as guard_controller, api as api_controller,
    )

    router = Router()
    # Create a new Router object.
    # The Router is responsible for matching incoming URLs and HTTP
    # requests to the correct controller route.
    router.register_blueprint(auth_controller.auth_bp, url_prefix="")
    # Register the authentication Blueprint.
    # url_prefix="" means its routes do not receive an additional prefix.
    # This handles authentication-related routes such as login/logout.
    router.register_blueprint(super_admin_controller.super_admin_bp, url_prefix="/super-admin")
    # Register the Super Admin Blueprint.
    # All routes belonging to the Super Admin controller are placed
    # under the /super-admin URL prefix.
    router.register_blueprint(admin_controller.admin_bp, url_prefix="/admin")
    # Register the location Admin Blueprint.
    # Its routes are placed under the /admin URL prefix.
    router.register_blueprint(guard_controller.guard_bp, url_prefix="/guard")
    # Register the Guard Blueprint.
    # Its routes are placed under the /guard URL prefix.
    router.register_blueprint(driver_controller.driver_bp, url_prefix="/driver")
    # Register the Driver Blueprint.
    # Its routes are placed under the /driver URL prefix.
    router.register_blueprint(api_controller.api_bp, url_prefix="")
    # Register the API Blueprint.
    # No additional URL prefix is added to these API routes.

    return router
    # Return the completed Router containing all registered
    # application routes.


def wire_controllers(router: Router, env, jinja_router) -> None:
    # Defines the wire_controllers function.
    # router: Router tells Python that the first parameter should be a Router.
    # env contains the Jinja template environment used to render HTML pages.
    # jinja_router contains the router used when generating URLs in templates.
    # -> None means this function does not return a value
    """
    Inject the render() helper and router into each controller module
    so they can render templates and build redirect URLs.
    """
    from controllers import (
        auth as auth_controller, driver as driver_controller, admin as admin_controller,
        super_admin as super_admin_controller, guard as guard_controller,
    )
    # Import the controllers that need the render helper and router.
    # "as" gives each imported module a shorter/local name.
    # For example, auth is referred to as auth_controller.

    def render(template_name: str, request: Request, context: dict) -> str:
        # Defines a small helper function called render.
        # template_name is the name of the HTML template to display.
        # request contains information about the current HTTP request.
        # context is a dictionary containing data that will be passed
        # from Python to the HTML template.
        # -> str means the function returns the rendered HTML as a string.
        return render_template(env, jinja_router, template_name, request, context)
        # Calls the application's render_template function.
        # It supplies the template environment, router, template name,
        # request, and context data.
        # The resulting HTML is returned to the controller.


    auth_controller.configure(render, router)
    # Gives the authentication controller access to the render helper
    # and the main application router.
    driver_controller.configure(render, router)
    # Gives the driver controller access to rendering and routing.
    admin_controller.configure(render, router)
    # Gives the location admin controller access to rendering and routing.
    super_admin_controller.configure(render, router)
    # Gives the Super Admin controller access to rendering and routing.
    guard_controller.configure(render, router)
    # Gives the Guard controller access to rendering and routing.
    auth_decorators.set_router(router)
    # Gives the authentication decorators access to the application's router.
    # This allows authentication/authorisation logic to generate or use
    # the correct routes when redirecting users.
    auth_decorators.set_render(render)
    # Gives the authentication decorators access to the render function.
    # This allows them to render pages when required by authentication
    # or authorisation logic.


# Built once at process startup, shared by every request/thread.
ROUTER = build_router()
# Build the application's main router once when the program starts.
# The router contains all registered routes for the system.
JINJA_ENV, _ = build_environment(ROUTER)
# Create the Jinja template environment using the configured router.
# Jinja is responsible for processing HTML templates and inserting # dynamic data into them.
# The underscore (_) means the second returned value is intentionally # not needed here.
wire_controllers(ROUTER, JINJA_ENV, ROUTER)
# Connect the controllers to the router and Jinja environment.
# This allows controllers to render templates and generate URLs.
# This setup is performed once during application startup.

def _content_type_for(path: Path) -> str:
    # Defines a function that determines the MIME/content type of a file.
    # path is expected to be a Path object representing the file.
    # -> str means the function returns the content type as text.
    guessed, _ = mimetypes.guess_type(str(path))
    # mimetypes.guess_type() attempts to determine the file's MIME type # based on its extension.
    # The first returned value is the guessed content type.
    # The second value is not needed, so it is stored in _.
    return guessed or "application/octet-stream"
    # Return the detected content type if one was found.
    # "or" provides a fallback value if guessed is None or otherwise false.
    # application/octet-stream is a general binary file content type.
    # This ensures the function always returns a usable content type.


def _serve_static(request_path: str) -> Response:
    # Defines a function for serving static files such as CSS,
    # JavaScript, images, and other files from the static directory.
    # request_path contains the requested URL path.
    # -> Response means the function returns an HTTP Response object.
    """
    Serve a file from static/ for paths starting with "/static/".
    """
    relative = request_path[len("/static/"):]
    file_path = (STATIC_DIR / relative).resolve()
    # Combine the static directory with the requested relative path.
    # .resolve() converts it into an absolute path.
    # This is important because the application needs to know the actual
    # file location before attempting to read it.

    # Reject any path that escapes the static directory (e.g. "../../config.py").
    # A malicious request could attempt to use ".." to move outside
    # the static directory and access sensitive application files.
    # If the resolved file is outside STATIC_DIR, the application
    # immediately returns a 404 Not Found response.
    if STATIC_DIR not in file_path.parents and file_path != STATIC_DIR:
        return Response.not_found()
    if not file_path.is_file():
        return Response.not_found()
    # Check whether the requested path actually points to a file.
    # If the file does not exist, return a 404 Not Found response.

    body = file_path.read_bytes()
    # Read the contents of the file as bytes.
    return Response(body, status=200, content_type=_content_type_for(file_path))
    # Create and return an HTTP response containing the file.
    # status=200 means the request was successful.
    # _content_type_for(file_path) determines the correct MIME type
    # so the browser knows how to interpret the file.


# Defines a function for displaying an error page.
# template_name is the HTML template for the error.
# status is the HTTP status code, such as 404 or 500.
# request contains information about the current request.
# The function returns an HTTP Response.
def _render_error_page(template_name: str, status: int, request: Request) -> Response:
    try:
        html = render_template(JINJA_ENV, ROUTER, template_name, request, {})
        # Render the requested error template using the Jinja environment.
        # {} is an empty dictionary because no additional template data
        # is being supplied to the error page.
        # The resulting HTML is stored in html
        return Response.html(html, status=status)
    except Exception:
        # If rendering the custom error page itself fails,
        # catch the exception instead of allowing the application to crash.
        return Response.html(f"<h1>{status}</h1>", status=status)
        # Provide a simple fallback HTML error page.
        # f"" is an f-string, allowing the value of status to be inserted
        # directly into the HTML.
        # This ensures the user still receives an error response even if
        # the custom error template cannot be rendered.


def handle_request(request: Request) -> Response:
    # Defines the main function responsible for processing an HTTP request.
    # It receives a Request object and returns a Response object.
    """
    Route and dispatch one request to a controller.
    Handles 404 (no route), 405 (wrong method), and 500 (exception).
    """
    load_session_into_request(request)
    # Load the user's existing session information into the request.
    # This allows the application to know information associated with
    # the current user, such as login/session data.

    try:
        # Ask the Router to find the correct route for the incoming request.
        # ROUTER.dispatch(request) checks the URL and HTTP method.
        # route receives the matching route.
        # path_params receives dynamic values extracted from the URL
        route, path_params = ROUTER.dispatch(request)
    except MethodNotAllowed:
        return Response.html("405 Method Not Allowed", status=405)
        # If the URL exists but the HTTP method being used is not allowed,
        # catch the MethodNotAllowed exception.
        # Return HTTP status 405 to tell the client that the method is not allowed.

    if route is None:
        # Check whether the Router failed to find a matching route.
        # None means no route was found.
        if request.path.startswith("/static/"):
            # If no normal route was found, check whether the request is
            # asking for a static file.
            # startswith() checks whether the URL begins with "/static/".
            return _serve_static(request.path)
            # If it is a static-file request, send it to the static-file
            # serving function.
        return _render_error_page("errors/404.html", 404, request)
        # If the request is not for a static file and no route exists,
        # render the application's 404 Not Found page.

    request.path_params = path_params
    # Store the parameters extracted from the URL inside the Request object.
    # The controller can then access these values when processing # the request.


    try:
        # Call the handler/controller function belonging to the matched route.
        # The current Request object is passed to the handler.
        # The handler performs the actual application operation and returns
        # an HTTP response.
        response = route.handler(request)
    except Exception:
        # This prevents an unhandled exception from crashing the request.
        print("[500 ERROR]")
        # Print a clear 500 error label to the server console for debugging.
        print(traceback.format_exc())
        # Print the complete traceback.
        # A traceback shows where the error occurred and helps the developer
        # identify and fix the problem.
        return _render_error_page("errors/500.html", 500, request)
        # Return the application's 500 Internal Server Error page
        # instead of exposing the technical error directly to the user.

    if not isinstance(response, StreamingResponse):
        # Check whether the response is NOT a streaming response.
        # isinstance() checks whether an object belongs to a particular class/type.
        save_session_from_request(request, response)
        # Save any session changes made while processing the request.
        # This allows updated session information to be preserved for
        # subsequent requests.

    return response
    # Return the final response to the HTTP server handler.
    # The response is then sent back to the user's browser.


class SmartParkRequestHandler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler is the Python HTTP server class being inherited
    # Inheritance allows SmartParkRequestHandler to use functionality
    # already provided by Python's HTTP server.
    """
    HTTP request handler: translates raw socket traffic into
    Request/Response objects and passes them through handle_request().
    """

    server_version = "SmartPark/1.0"

    # Defines the server's version string.
    # This identifies the application as SmartPark version 1.0
    # when the HTTP server reports its server information.

    def _read_body(self) -> bytes:
        # Defines a helper method for reading the body of an HTTP request.
        # -> bytes means the method returns binary data.
        length = int(self.headers.get("Content-Length", 0) or 0)
        # Content-Length tells the server how many bytes are contained
        # in the request body. # self.headers.get("Content-Length", 0) # attempts to obtain the header # If it does not exist, 0 is used.
        return self.rfile.read(length) if length else b""
        # If length contains a value greater than zero,
        # read exactly that number of bytes from the request stream.
        # self.rfile is the input stream containing the HTTP request body. # b"" represents an empty bytes value.


    def _handle(self, method: str) -> None:
        # Defines the main helper method for processing an HTTP request.
        # method contains the HTTP method being handled, such as GET or POST.
        # -> None means this method does not directly return a value.
        body = self._read_body()
        # Read the body of the incoming HTTP request.
        # The result is stored in body.
        client_ip = self.client_address[0] if self.client_address else ""
        # Obtain the IP address of the client making the request.
        # self.client_address contains information about the connected client.
        # [0] selects the client's IP address.
        request = Request(method, self.path, self.headers, body, remote_addr=client_ip)
        # Create the application's Request object
        # self.path = requested URL path.
        # self.headers = HTTP request headers.
        # body = request body that was read earlier.
        # remote_addr = client's IP address.
        # This converts the low-level HTTP request into the Request
        # structure used by the rest of the SmartPark application.
        try:
            response = handle_request(request)
            # Send the Request object to the main handle_request() function.
            # That function performs routing, authentication/session handling,
            # controller execution, and error handling.
        except Exception:
            print("[FATAL HANDLER ERROR]")
            # Print a clear message to the server console indicating
            # that a serious request-handler error occurred.
            print(traceback.format_exc())
            # Print the complete error traceback to help the developer
            # diagnose the problem.
            response = Response.html("500 Internal Server Error", status=500)
            # If request processing completely fails, create a basic
            # HTTP 500 response instead of leaving the client without a response.
            # The browser receives "500 Internal Server Error".

        self._write_response(response)
        # Send the final Response object back to the client/browser.
        # _write_response() converts the application's Response into
        # the actual HTTP response sent over the network.

    def _write_response(self, response) -> None:
        if isinstance(response, StreamingResponse):
            # Check whether the response sends data in separate chunks.
            self.send_response(response.status)
            # Send the HTTP status code to the client.
            self.send_header("Content-Type", response.content_type)
            # Tell the client the type of content being sent.
            self.end_headers()
            # Finish sending the response headers.
            try:
                # Send each piece of the streaming response to the client.
                for chunk in response.chunks_iterable:
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Ignore errors caused when the client closes the connection early
                pass
            # Stop here because the streaming response is already complete.
            return

        self.send_response(response.status)
        # Send the HTTP status code for a normal response.
        for key, value in response.headers.items():
            self.send_header(key, value)
        # Send all headers stored in the response.
        for cookie_str in response.set_cookies:
            self.send_header("Set-Cookie", cookie_str)
        # Send any cookies that need to be stored by the client's browser.
        self.send_header("Content-Length", str(len(response.body)))
        # Tell the client the exact size of the response body in bytes.
        self.end_headers()
        # Finish sending the response headers.
        try:
            # Send the actual response body to the client's browser.
            self.wfile.write(response.body)
        # Ignore errors if the client disconnects before receiving the response.
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        # Handle incoming GET requests using the main request handler.
        self._handle("GET")

    def do_POST(self):
        # Handle incoming POST requests using the main request handler.
        self._handle("POST")

    def do_PUT(self):
        # Handle incoming PUT requests using the main request handler.
        self._handle("PUT")

    def do_PATCH(self):
        # Handle incoming PATCH requests using the main request handler.
        self._handle("PATCH")

    def do_DELETE(self):
        # Handle incoming DELETE requests using the main request handler.
        self._handle("DELETE")

    def log_message(self, fmt, *args):
        # Only display HTTP server logs when debug mode is enabled.
        if Config.DEBUG:
            super().log_message(fmt, *args)


class _Server(ThreadingHTTPServer):
    """One-thread-per-request HTTP server. daemon_threads ensures
    clean exit on Ctrl+C."""
    daemon_threads = True
    # Allow each incoming request to be handled in its own thread.
    allow_reuse_address = True
    # Allow the server to reuse its address after restarting.


def main() -> None:
    # Display the name of the SmartPark application when it starts.
    print("  smart.park — parking management system")


    # Create and initialise the application's database.
    init_db()
    print(f"  Database ready at: {Config.DATABASE_PATH}")
    # Display the location of the database being used.

    # Create the HTTP server using the configured host, port,
    # and SmartPark request handler.
    server = _Server((Config.HOST, Config.PORT), SmartParkRequestHandler)
    print(f"  Listening on http://{Config.HOST}:{Config.PORT}")
    # Display the address where the server is listening.


    try:
        # Keep the server running and continuously accept requests.
        server.serve_forever()
    except KeyboardInterrupt:
        # Allow the server to be stopped safely with Ctrl+C.
        print("\nShutting down...")
        server.shutdown()
        # Stop the server and finish active server operations cleanly.

# Only start the application when this file is run directly.
# It will not automatically start when imported by another module.
if __name__ == "__main__":
    main()
