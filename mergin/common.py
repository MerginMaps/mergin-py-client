import os
from enum import Enum

CHUNK_SIZE = 100 * 1024 * 1024

# there is an upper limit for chunk size on server, ideally should be requested from there once implemented
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024


this_dir = os.path.dirname(os.path.realpath(__file__))


# Error code from the public API, add to the end of enum as we handle more eror
class ErrorCode(Enum):
    ProjectsLimitHit = "ProjectsLimitHit"
    StorageLimitHit = "StorageLimitHit"


class ClientError(Exception):
    def __init__(self, detail, url=None, server_code=None, server_response=None, http_error=None, http_method=None):
        self.detail = detail
        self.url = url
        self.http_error = http_error
        self.http_method = http_method

        self.server_code = server_code
        self.server_response = server_response

        self.extra = None

    def __str__(self):
        string_res = f"Detail: {self.detail}\n"
        if self.http_error:
            string_res += f"HTTP Error: {self.http_error}\n"
        if self.url:
            string_res += f"URL: {self.url}\n"
        if self.http_method:
            string_res += f"Method: {self.http_method}\n"
        if self.extra:
            string_res += f"{self.extra}\n"
        return string_res


class LoginError(Exception):
    pass


class InvalidProject(Exception):
    pass


try:
    import dateutil.parser
    from dateutil.tz import tzlocal
except ImportError:
    # this is to import all dependencies shipped with package (e.g. to use in qgis-plugin)
    deps_dir = os.path.join(this_dir, "deps")
    if os.path.exists(deps_dir):
        import sys

        for f in os.listdir(os.path.join(deps_dir)):
            sys.path.append(os.path.join(deps_dir, f))

        import dateutil.parser
        from dateutil.tz import tzlocal
