"""Admin CLI for the portal.

Usage:
    python -m portal.cli reset-password <email>
    python -m portal.cli list-users
"""
import argparse
import getpass
import sys

from sqlmodel import Session, select

from portal.db import engine, init_db
from portal.models import User
from portal.security import hash_password, validate_password


def reset_password(email: str) -> int:
    init_db()
    with Session(engine) as db:
        user = db.exec(select(User).where(User.email == email.lower())).first()
        if user is None:
            print(f"No user found with email {email}", file=sys.stderr)
            return 1
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm:      ")
        if password != confirm:
            print("Passwords do not match", file=sys.stderr)
            return 1
        errors = validate_password(password)
        if errors:
            for e in errors:
                print(e, file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        db.add(user)
        db.commit()
        print(f"Password updated for {user.email}")
        return 0


def list_users() -> int:
    init_db()
    with Session(engine) as db:
        users = db.exec(select(User)).all()
        if not users:
            print("No users.")
            return 0
        for u in users:
            print(f"  {u.email}  ({u.role})  created {u.created_at:%Y-%m-%d}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="portal")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_reset = sub.add_parser("reset-password", help="Reset a user's password")
    p_reset.add_argument("email")
    sub.add_parser("list-users", help="List all users")

    args = parser.parse_args()
    if args.cmd == "reset-password":
        return reset_password(args.email)
    if args.cmd == "list-users":
        return list_users()
    return 2


if __name__ == "__main__":
    sys.exit(main())
