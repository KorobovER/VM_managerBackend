
# Backend (FastAPI, KVM-only)

## Установка
pip install -r requirements.txt
sudo apt install sshpass

## Запуск
uvicorn main:app --host 0.0.0.0 --port 9920

## Важно!
- Для /vms/available используется sshpass и ssh (логин+пароль передаёт фронт)
- На целевом сервере должен быть qemu/kvm, виртуалки должны запускаться как процессы qemu
