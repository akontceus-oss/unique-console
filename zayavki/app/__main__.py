import argparse
import getpass
import sys

from sqlalchemy import select


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="запустить сервер")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8100)
    cu = sub.add_parser("create-user", help="создать пользователя (первого владельца, администратора)")
    cu.add_argument("--login", required=True)
    cu.add_argument("--name", required=True)
    cu.add_argument("--role", default="owner", choices=["owner", "admin", "manager"])
    cu.add_argument("--password", help="если не указан, спросит")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .main import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    elif args.cmd == "create-user":
        from .auth import hash_password
        from .db import init_engine, session_scope
        from .models import User

        init_engine()
        password = args.password or getpass.getpass("Пароль: ")
        if len(password) < 6:
            sys.exit("Пароль от 6 символов")
        with session_scope() as s:
            if s.scalar(select(User).where(User.login == args.login.lower())):
                sys.exit("Такой логин уже есть")
            s.add(User(login=args.login.lower(), name=args.name, role=args.role, password_hash=hash_password(password)))
        print("Готово:", args.login, args.role)


if __name__ == "__main__":
    main()
