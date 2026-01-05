#!/bin/bash
ssh -o StrictHostKeyChecking=no -L 14444:172.20.0.198:16509 gate@212.192.123.3 -p 3003 -N