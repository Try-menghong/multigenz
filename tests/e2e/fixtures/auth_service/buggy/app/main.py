"""FastAPI authentication service: register, login and a protected /me endpoint."""

import os

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app import security

SECRET = os.environ.get("AUTH_SECRET", "development-only-signing-key")
USERS: dict[str, str] = {}

app = FastAPI(title="Auth service")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


def current_user(authorization: str | None = Header(default=None)) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    subject = security.decode_token(value, SECRET) if scheme.lower() == "bearer" else None
    if subject is None or subject not in USERS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token",
                            headers={"WWW-Authenticate": "Bearer"})
    return subject


@app.post("/register", status_code=status.HTTP_201_CREATED)
def register(creds: Credentials) -> dict[str, str]:
    if creds.username in USERS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username taken")
    USERS[creds.username] = security.hash_password(creds.password)
    return {"username": creds.username}


@app.post("/login", response_model=Token)
def login(creds: Credentials) -> Token:
    stored = USERS.get(creds.username)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad credentials")
    return Token(access_token=security.create_token(creds.username, SECRET))


@app.get("/me")
def me(user: str = Depends(current_user)) -> dict[str, str]:
    return {"username": user}
