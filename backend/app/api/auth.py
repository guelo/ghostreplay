from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import create_access_token, decode_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)


class RegisterResponse(BaseModel):
    token: str
    user_id: int
    username: str


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)


class LoginResponse(BaseModel):
    token: str
    user_id: int
    username: str


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(
    request: RegisterRequest,
    db: Session = Depends(get_db),
) -> RegisterResponse:
    """
    Register a new anonymous user.

    Creates a user with the provided credentials and returns a JWT token.
    Frontend auto-generates credentials on first visit.
    """
    password_hash = hash_password(request.password)

    user = User(
        username=request.username,
        password_hash=password_hash,
        is_anonymous=True,
    )

    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    token = create_access_token(
        user_id=user.id,
        username=user.username,
        is_anonymous=user.is_anonymous,
    )

    return RegisterResponse(
        token=token,
        user_id=user.id,
        username=user.username,
    )


@router.post("/login", response_model=LoginResponse)
def login(
    request: LoginRequest,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """
    Validate credentials and return a JWT token.
    """
    user = db.query(User).filter(User.username == request.username).first()
    if user is None or not verify_password(request.password, user.password_hash or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    token = create_access_token(
        user_id=user.id,
        username=user.username,
        is_anonymous=user.is_anonymous,
    )

    return LoginResponse(
        token=token,
        user_id=user.id,
        username=user.username,
    )


class ClaimRequest(BaseModel):
    new_username: str = Field(..., min_length=3, max_length=50)
    new_password: str = Field(..., min_length=6)


class ClaimResponse(BaseModel):
    token: str
    user_id: int
    username: str


@router.post("/claim", response_model=ClaimResponse)
def claim(
    request: ClaimRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(...),
) -> ClaimResponse:
    """
    Upgrade an anonymous account to a claimed account.

    Requires a valid JWT for an anonymous user. Sets a new username and
    password, then returns a fresh token with updated claims.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )

    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user = db.query(User).filter(User.id == payload.user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if not user.is_anonymous:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account already claimed",
        )

    user.username = request.new_username
    user.password_hash = hash_password(request.new_password)
    user.is_anonymous = False

    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )

    new_token = create_access_token(
        user_id=user.id,
        username=user.username,
        is_anonymous=user.is_anonymous,
    )

    return ClaimResponse(
        token=new_token,
        user_id=user.id,
        username=user.username,
    )
