import os
import requests
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt

KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://keycloak:8080")
REALM = os.getenv("KEYCLOAK_REALM", "master")

security = HTTPBearer()

def get_keycloak_public_key():
    url = f"{KEYCLOAK_URL}/realms/{REALM}"
    res = requests.get(url).json()
    public_key = res.get("public_key")
    return f"-----BEGIN PUBLIC KEY-----\n{public_key}\n-----END PUBLIC KEY-----"

def verify_jwt(credentials: HTTPAuthorizationCredentials = Security(security)):
    token = credentials.credentials
    try:
        public_key = get_keycloak_public_key()
        payload = jwt.decode(
            token, 
            public_key, 
            algorithms=["RS256"], 
            options={"verify_aud": False}
        )
        return payload
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Ungültiger Token: {str(e)}")