# Course Payment Bot

## 1. Bot banao (BotFather)
1. Telegram me `@BotFather` kholo, `/newbot` bhejo, naam aur username do.
2. Jo token milega wo `BOT_TOKEN` hai.

## 2. Apna Telegram User ID pata karo
`@userinfobot` ko koi bhi message bhejo, wo tumhara numeric ID bata dega. Yahi `ADMIN_ID` hai — sirf ye ID bot ko commands chala payegi.

## 3. MongoDB Atlas setup (free)
1. https://www.mongodb.com/cloud/atlas/register par free account banao
2. Free "M0" cluster create karo
3. Database Access me ek user banao (username/password)
4. Network Access me "Allow access from anywhere" (0.0.0.0/0) add karo — Heroku ka IP fixed nahi hota
5. "Connect" → "Drivers" se connection string copy karo, ye format hoga:
   `mongodb+srv://user:password@cluster.mongodb.net/`
   Ye tumhara `MONGODB_URI` hai.

## 4. Heroku par deploy
```bash
heroku login
heroku create your-app-name

# Apt buildpack (tesseract ke liye) + python buildpack, dono order me
heroku buildpacks:add --index 1 heroku-community/apt
heroku buildpacks:add --index 2 heroku/python

# Environment variables set karo
heroku config:set BOT_TOKEN=your_bot_token
heroku config:set ADMIN_ID=your_numeric_user_id
heroku config:set MONGODB_URI="mongodb+srv://user:password@cluster.mongodb.net/"

git init
git add .
git commit -m "course bot"
git push heroku main

# Worker dyno on karo (bot polling se chalta hai, web dyno nahi worker dyno chahiye)
heroku ps:scale worker=1
