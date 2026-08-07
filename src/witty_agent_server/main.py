from uvicorn import run

from witty_agent_server.app import create_app


def main() -> None:
    # 服务需对外监听，端口由部署层控制
    run(create_app(), host="0.0.0.0", port=8000)  # nosec B104


if __name__ == "__main__":
    main()
