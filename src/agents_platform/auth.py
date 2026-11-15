"""Cognito access-token verification and workflow role checks."""

from dataclasses import dataclass

import jwt
from fastapi import HTTPException


@dataclass(frozen=True)
class Principal:
    subject: str
    groups: frozenset[str]


class CognitoVerifier:
    def __init__(self, region: str, user_pool_id: str, app_client_id: str) -> None:
        if not all((region, user_pool_id, app_client_id)):
            raise ValueError("Cognito region, user pool, and app client are required")
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self.app_client_id = app_client_id
        self.jwks = jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json")

    def verify(self, token: str) -> Principal:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={
                    "verify_aud": False,
                    "require": ["exp", "iss", "sub", "client_id", "token_use"],
                },
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="Invalid access token") from exc
        if claims["token_use"] != "access" or claims["client_id"] != self.app_client_id:
            raise HTTPException(status_code=401, detail="Invalid access token")
        return Principal(
            subject=claims["sub"], groups=frozenset(claims.get("cognito:groups", []))
        )
