"""Entry point: ``python -m mcp_servers.crm_server``."""

from mcp_servers.common.runtime import run_server_cli
from mcp_servers.crm_server.server import server


def main() -> None:
    run_server_cli(server, default_port=9001)


if __name__ == "__main__":
    main()
