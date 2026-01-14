from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional, List
from enum import Enum

class RoleEnum(str, Enum):
    admin = "admin"
    user = "user"

class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: RoleEnum = RoleEnum.user

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str

class ServerCreate(BaseModel):
    name: str
    host: str
    port: int = 16509
    user: str

class ServerResponse(BaseModel):
    id: int
    name: str
    host: str
    port: int
    user: str
    created_at: datetime

    class Config:
        from_attributes = True

class VMCreate(BaseModel):
    name: str
    server_id: int
    ip_address: str
    rdp_port: int = 3389
    video_port: int = 55554

class VMResponse(BaseModel):
    id: int
    name: str
    server_id: int
    ip_address: str
    rdp_port: int
    video_port: int
    created_at: datetime

    class Config:
        from_attributes = True

class JumpHostCreate(BaseModel):
    name: str
    host: str
    port: int = 22
    user: str

class JumpHostResponse(BaseModel):
    id: int
    name: str
    host: str
    port: int
    user: str
    created_at: datetime

    class Config:
        from_attributes = True

class VMHostParams(BaseModel):
    host: str
    port: int = 16509

class VMActionRequest(BaseModel):
    vm_host: str
    vm_name: str
    host: str
    port: int = 16509
