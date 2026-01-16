from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Enum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from .database import Base

class RoleEnum(str, enum.Enum):
    admin = "admin"
    user = "user"

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(Enum(RoleEnum), default=RoleEnum.user)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    vms = relationship("UserVM", back_populates="user")

class Server(Base):
    __tablename__ = "servers"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    host = Column(String)
    port = Column(Integer, default=16509)
    user = Column(String)
    jump_host_id = Column(Integer, ForeignKey("jump_hosts.id"), nullable=True)
    local_tunnel_port = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    deleted = Column(Integer, default=0)
    vms = relationship("VM", back_populates="server")
    jump_host = relationship("JumpHost")

class VM(Base):
    __tablename__ = "vms"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    server_id = Column(Integer, ForeignKey("servers.id"))
    ip_address = Column(String)
    rdp_port = Column(Integer, default=3389)
    video_port = Column(Integer, default=55554)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    server = relationship("Server", back_populates="vms")
    user_vms = relationship("UserVM", back_populates="vm")

class UserVM(Base):
    __tablename__ = "user_vms"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    vm_id = Column(Integer, ForeignKey("vms.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="vms")
    vm = relationship("VM", back_populates="user_vms")

class JumpHost(Base):
    __tablename__ = "jump_hosts"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    host = Column(String)
    port = Column(Integer, default=22)
    user = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
