# Deploy ขึ้น Render แบบไม่ต้องเปิดคอม

ชุดนี้พร้อม deploy เป็น web service แล้ว

## ไฟล์ที่ Render ใช้

- `render.yaml`
- `requirements.txt`
- `.python-version`
- `Procfile`
- `tradingview_telegram_webhook.py`

## วิธี deploy

1. เอาโฟลเดอร์นี้ขึ้น GitHub
2. เข้า Render
3. เลือก `New +`
4. เลือก `Blueprint`
5. เลือก repo ที่มีไฟล์ `render.yaml`
6. กด deploy

หลังจาก service ถูกสร้างแล้ว ให้ตั้ง env เพิ่มถ้ายังไม่ครบ:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

ค่า `TRADINGVIEW_WEBHOOK_SECRET` Render จะสร้างให้จาก `render.yaml`

## หลัง deploy เสร็จ

Render จะให้ URL ประมาณนี้:

```text
https://tradingview-telegram-webhook.onrender.com
```

Webhook URL ที่เอาไปใส่ใน TradingView คือ:

```text
https://tradingview-telegram-webhook.onrender.com/webhook/tradingview
```

Health check:

```text
https://tradingview-telegram-webhook.onrender.com/healthz
```

## ตั้งใน TradingView

1. เปิด `TradingView_Market_Prediction_Alert_v1.pine`
2. Add to chart
3. เปิดค่าของ indicator
4. ใส่ `Webhook Secret` ให้ตรงกับ `TRADINGVIEW_WEBHOOK_SECRET` บน Render
5. Create Alert
6. Condition = `Any alert() function call`
7. Frequency = `Once Per Bar Close`
8. เปิด `Webhook URL`
9. ใส่ URL ของ Render

## หมายเหตุ

- ตัว receiver ฟังที่ `0.0.0.0:$PORT` ตามแนวทางของ Render/Railway
- TradingView ยิงเข้า `localhost` ไม่ได้ ต้องใช้ public HTTPS URL
