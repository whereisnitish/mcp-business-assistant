"""Entry point: ``python -m mcp_servers.task_server``."""

from mcp_servers.common.runtime import run_server_cli
from mcp_servers.task_server.server import server


def main() -> None:
    run_server_cli(server, default_port=9002)


if __name__ == "__main__":
    main()
