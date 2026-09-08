"""Entry point: ``python -m mcp_servers.calendar_server``."""

from mcp_servers.calendar_server.server import server
from mcp_servers.common.runtime import run_server_cli


def main() -> None:
    run_server_cli(server, default_port=9005)


if __name__ == "__main__":
    main()
