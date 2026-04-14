# TradingView -> Telegram Alerts

ชุดนี้เพิ่ม 2 ส่วนให้พร้อมใช้:

- Pine Script สำหรับสัญญาณแบบคัดเข้มและยิง webhook ตอนแท่งปิด
- Python webhook receiver ที่รับ alert จาก TradingView แล้วส่งต่อเข้า Telegram

## ไฟล์สำคัญ

- `TradingView_Market_Prediction_Alert_v1.pine`
- `tradingview_telegram_webhook.py`
- `start_tradingview_webhook.cmd`
- `status_tradingview_webhook.cmd`
- `stop_tradingview_webhook.cmd`

## แนวคิดของสัญญาณ

สคริปต์นี้ไม่ได้การันตีว่าแม่น 100% แต่ตั้งใจคัดสัญญาณที่ "สะอาด" มากขึ้น โดยรวมเงื่อนไขเหล่านี้:

- EMA trend alignment
- Higher-timeframe trend confirm
- ADX + DI confirm
- MACD momentum confirm
- RSI อยู่ใน sweet spot ไม่ไล่จุด overbought/oversold เกินไป
- Volume boost
- แท่งเทียนปิดแข็งแรง
- ราคาไม่ยืดห่างจาก EMA มากเกินไป
- ยิง alert ตอนแท่งปิดเท่านั้น

## เริ่มใช้ Telegram webhook receiver

1. เปิดไฟล์ `.env`
2. ตรวจว่ามี `TELEGRAM_BOT_TOKEN` และ `TELEGRAM_CHAT_ID` แล้ว
3. ถ้าต้องการล็อก webhook ให้เพิ่มค่า:

```text
TRADINGVIEW_WEBHOOK_SECRET=your_secret_here
```

4. รันตัวรับ webhook:

```powershell
.\start_tradingview_webhook.cmd
```

ถ้าอยากลองก่อนโดยยังไม่ส่งเข้าจริง:

```powershell
.\start_tradingview_webhook.cmd -DryRun
```

เช็กสถานะ:

```powershell
.\status_tradingview_webhook.cmd
```

หยุด:

```powershell
.\stop_tradingview_webhook.cmd
```

ค่า default ของ receiver:

- local URL: `http://127.0.0.1:8787/webhook/tradingview`
- health check: `http://127.0.0.1:8787/healthz`

## ให้ TradingView ยิงเข้ามาได้จากภายนอก

TradingView ยิง webhook หา `localhost` ไม่ได้โดยตรง ต้องมี public HTTPS URL อยู่ข้างหน้า เช่น:

- deploy สคริปต์นี้ขึ้น VPS / Railway / Render
- หรือเปิด tunnel เช่น Cloudflare Tunnel ไปยัง `http://127.0.0.1:8787`

ตัวอย่างถ้าใช้ Cloudflare Tunnel:

```powershell
cloudflared tunnel --url http://127.0.0.1:8787
```

แล้วเอา URL ที่ได้มาต่อ path:

```text
https://your-public-subdomain.trycloudflare.com/webhook/tradingview
```

## ตั้งค่า Pine Script ใน TradingView

1. เปิด TradingView Pine Editor
2. วางโค้ดจาก `TradingView_Market_Prediction_Alert_v1.pine`
3. กด Add to chart
4. ตั้งค่า `Webhook Secret` ในอินดิเคเตอร์ให้ตรงกับ `TRADINGVIEW_WEBHOOK_SECRET` ถ้าใช้ secret
5. กด Create Alert
6. ในช่อง Condition ให้เลือก:

```text
Any alert() function call
```

7. Frequency แนะนำ:

```text
Once Per Bar Close
```

8. ติ๊ก `Webhook URL`
9. ใส่ public webhook URL ของเรา
10. Message ไม่ต้องแก้ ถ้าใช้ `alert()` จากสคริปต์นี้

## หมายเหตุสำคัญ

- TradingView ใช้ webhook ได้เมื่อบัญชีเปิด 2FA แล้ว
- ถ้า message เป็น JSON ที่ถูกต้อง TradingView จะส่ง `application/json`
- ฝั่ง TradingView ต้องมองเห็น URL ผ่าน HTTPS ที่ใช้งานได้จริง
- ถ้าจะเปิด public จริง แนะนำตั้ง `TRADINGVIEW_WEBHOOK_SECRET`
