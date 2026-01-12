# -*- coding: utf-8 -*-
from db.database import engine, Base, SessionLocal
from db.models import User, Server, JumpHost, RoleEnum
from auth import get_password_hash

def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    
    admin_user = db.query(User).filter(User.username == "admin").first()
    if not admin_user:
        admin_user = User(
            username="root",
            email="root@root.root",
            hashed_password=get_password_hash("root"),
            role=RoleEnum.admin,
            is_active=True
        )
        db.add(admin_user)
        print("✓ Created admin user (username: root, password: root)")
    
    test_user = db.query(User).filter(User.username == "user").first()
    if not test_user:
        test_user = User(
            username="user",
            email="user@cloudgaming.local",
            hashed_password=get_password_hash("user"),
            role=RoleEnum.user,
            is_active=True
        )
        db.add(test_user)
        print("✓ Created test user (username: user, password: user)")
    
    jump_host = db.query(JumpHost).filter(JumpHost.name == "default").first()
    if not jump_host:
        jump_host = JumpHost(
            name="default",
            host="212.192.123.3",
            port=3003,
            user="gate"
        )
        db.add(jump_host)
        print("✓ Created default jump host")
    
    server1 = db.query(Server).filter(Server.name == "VM Host 1").first()
    if not server1:
        server1 = Server(
            name="VM Host 1",
            host="172.20.0.198",
            port=16509,
            user="ggga1"
        )
        db.add(server1)
        print("✓ Created VM Host 1")
    
    server2 = db.query(Server).filter(Server.name == "VM Host 2").first()
    if not server2:
        server2 = Server(
            name="VM Host 2",
            host="172.20.0.199",
            port=16509,
            user="ggga2"
        )
        db.add(server2)
        print("✓ Created VM Host 2")
    
    db.commit()
    db.close()
    print("\n✓ Database initialized successfully!")

if __name__ == "__main__":
    init_db()
