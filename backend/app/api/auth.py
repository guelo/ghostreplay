from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)


class RegisterResponse(BaseModel):
    token: str
    user_id: int
    username: str


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)


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
