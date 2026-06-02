import os
import io
import json
import requests
import threading
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, FileMessage, ImageMessage, VideoMessage, AudioMessage,
    TextMessage, TextSendMessage, FlexSendMessage, QuickReply, QuickReplyButton,
    MessageAction, PostbackAction
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
ROOT_FOLDER_ID = os.environ.get("ROOT_FOLDER_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# State เก็บ pending file รอเลือกโฟลเดอร์ {userId: {messageId, fileName, groupId, fileType, senderShort, pushTo}}
pending_files = {}

BKK = pytz.timezone("Asia/Bangkok")

# ===================================================
# Google Drive
# ===================================================
def get_drive_service():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_or_create_folder(name, parent_id):
    service = get_drive_service()
    query = f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id"
    ).execute()
    return folder["id"]

def get_subfolders(group_folder_id):
    service = get_drive_service()
    query = f"'{group_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)", orderBy="name").execute()
    return [f["name"] for f in results.get("files", [])]

def upload_file(content: bytes, filename: str, folder_id: str, mimetype: str = "application/octet-stream"):
    service = get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mimetype, resumable=True)
    file = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink"
    ).execute()
    service.permissions().create(
        fileId=file["id"],
        body={"type": "anyone", "role": "reader"}
    ).execute()
    return file.get("webViewLink", "")

def search_files_by_date(group_folder_id, date_str):
    service = get_drive_service()
    start = f"{date_str}T00:00:00+07:00"
    end = f"{date_str}T23:59:59+07:00"
    query = f"'{group_folder_id}' in parents and createdTime >= '{start}' and createdTime <= '{end}' and trashed=false and mimeType != 'application/vnd.google-apps.folder'"
    results = service.files().list(q=query, fields="files(id, name, webViewLink, createdTime)", orderBy="createdTime desc", pageSize=5).execute()
    return results.get("files", [])

def search_files_recursive(folder_id, date_str=None, name_query=None, max_results=5):
    service = get_drive_service()
    conditions = [f"trashed=false", f"mimeType != 'application/vnd.google-apps.folder'"]
    if date_str:
        conditions.append(f"createdTime >= '{date_str}T00:00:00+07:00'")
        conditions.append(f"createdTime <= '{date_str}T23:59:59+07:00'")
    if name_query:
        conditions.append(f"name contains '{name_query}'")
    
    all_files = []
    
    def search_in_folder(fid):
        q = f"'{fid}' in parents and " + " and ".join(conditions)
        res = service.files().list(q=q, fields="files(id, name, webViewLink, createdTime)", pageSize=max_results).execute()
        all_files.extend(res.get("files", []))
        
        sub_q = f"'{fid}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        sub_res = service.files().list(q=sub_q, fields="files(id)").execute()
        for sub in sub_res.get("files", []):
            if len(all_files) < max_results:
                search_in_folder(sub["id"])
    
    search_in_folder(folder_id)
    return all_files[:max_results]

# ===================================================
# LINE Helpers
# ===================================================
def get_group_name(group_id):
    if not group_id:
        return "แชทส่วนตัว"
    try:
        profile = line_bot_api.get_group_summary(group_id)
        return profile.group_name
    except:
        return f"กลุ่ม_{group_id[-6:]}"

def get_sender_name(user_id, group_id):
    try:
        if group_id:
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
        else:
            profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except:
        return "Unknown"

def push_text(to, text):
    line_bot_api.push_message(to, TextSendMessage(text=text))

def push_flex(to, file_name, sender_short, group_name, sub_folder_name, folder_url, file_url, upload_time, file_type):
    emoji_map = {"image": "🖼️", "video": "🎬", "audio": "🎵", "file": "📄"}
    type_emoji = emoji_map.get(file_type, "📁")
    
    flex_content = {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1B5E20", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "บันทึกไฟล์สำเร็จแล้ว!", "color": "#FFFFFF", "weight": "bold", "size": "md"},
                {"type": "text", "text": f"{type_emoji} {file_name}", "color": "#A5D6A7", "size": "sm", "margin": "sm", "wrap": True}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "16px", "spacing": "sm",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "กลุ่ม:", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": group_name, "color": "#333333", "size": "sm", "flex": 5, "wrap": True}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "โฟลเดอร์:", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": f"📁 {sub_folder_name}", "color": "#1B5E20", "size": "sm", "flex": 5, "wrap": True}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "ส่งโดย:", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": sender_short, "color": "#333333", "size": "sm", "flex": 5, "wrap": True}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "เวลา:", "color": "#888888", "size": "sm", "flex": 2},
                    {"type": "text", "text": upload_time, "color": "#333333", "size": "sm", "flex": 5}
                ]},
                {"type": "text", "text": "อัปโหลดไปยัง Google Drive แล้ว ✅", "color": "#2E7D32", "size": "xs", "margin": "md"}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "action": {"type": "uri", "label": "📂 เปิดดูโฟลเดอร์", "uri": folder_url}, "style": "primary", "color": "#1B5E20", "height": "sm"},
                {"type": "button", "action": {"type": "uri", "label": "📄 เปิดไฟล์นี้", "uri": file_url}, "style": "secondary", "height": "sm"}
            ]
        }
    }
    line_bot_api.push_message(to, FlexSendMessage(alt_text=f"✅ บันทึกไฟล์สำเร็จ! {file_name}", contents=flex_content))

def reply_search_results(reply_token, query, files):
    if not files:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"🔍 ไม่พบไฟล์ที่ค้นหา: {query}"))
        return
    bubbles = []
    for f in files[:5]:
        created = f.get("createdTime", "")[:10]
        bubbles.append({
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": "📄", "size": "xxl", "align": "center"},
                    {"type": "text", "text": f["name"], "size": "xs", "wrap": True, "weight": "bold", "margin": "sm", "align": "center"},
                    {"type": "text", "text": created, "size": "xxs", "color": "#999999", "align": "center"}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "button", "action": {"type": "uri", "label": "เปิดไฟล์", "uri": f["webViewLink"]}, "style": "primary", "color": "#1B5E20", "height": "sm"}]
            }
        })
    line_bot_api.reply_message(reply_token, FlexSendMessage(
        alt_text=f"🔍 ผลการค้นหา: {query}",
        contents={"type": "carousel", "contents": bubbles}
    ))

# ===================================================
# Upload in background thread
# ===================================================
def do_upload(push_to, sub_folder_name, pending):
    try:
        group_folder_id = get_or_create_folder(pending["groupName"], ROOT_FOLDER_ID)
        sub_folder_id = get_or_create_folder(sub_folder_name, group_folder_id)
        
        content = line_bot_api.get_message_content(pending["messageId"])
        file_data = b"".join(chunk for chunk in content.iter_content())
        
        file_url = upload_file(file_data, pending["fileName"], sub_folder_id)
        folder_url = f"https://drive.google.com/drive/folders/{sub_folder_id}"
        now = datetime.now(BKK).strftime("%Y-%m-%d %H:%M")
        
        push_flex(push_to, pending["fileName"], pending["senderShort"],
                  pending["groupName"], sub_folder_name, folder_url, file_url, now, pending["fileType"])
    except Exception as e:
        push_text(push_to, f"❌ อัปโหลดไม่สำเร็จ: {str(e)}")

# ===================================================
# Webhook
# ===================================================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ===================================================
# Text Handler
# ===================================================
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    group_id = getattr(event.source, "group_id", None)
    push_to = group_id or user_id
    reply_token = event.reply_token

    # ถ้ามี pending file รอเลือกโฟลเดอร์
    if user_id in pending_files:
        pending = pending_files[user_id]
        
        if text == "➕ สร้างโฟลเดอร์ใหม่":
            pending["waitingForFolderName"] = True
            line_bot_api.reply_message(reply_token, TextSendMessage(text="📝 พิมพ์ชื่อโฟลเดอร์ย่อยที่ต้องการสร้าง:"))
            return
        
        if pending.get("waitingForFolderName"):
            folder_name = text
            del pending_files[user_id]
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"⏳ กำลังสร้างโฟลเดอร์ \"{folder_name}\" และอัปโหลด..."))
            threading.Thread(target=do_upload, args=(push_to, folder_name, pending)).start()
            return
        
        if text.startswith("📁 "):
            folder_name = text.replace("📁 ", "").strip()
            del pending_files[user_id]
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"⏳ กำลังอัปโหลดไปยัง 📁 {folder_name}..."))
            threading.Thread(target=do_upload, args=(push_to, folder_name, pending)).start()
            return

    # คำสั่งค้นหา
    if text == "ค้นหา":
        group_name = get_group_name(group_id)
        group_folder_id = get_or_create_folder(group_name, ROOT_FOLDER_ID)
        sub_folders = get_subfolders(group_folder_id)
        items = [QuickReplyButton(action=MessageAction(label="📅 ไฟล์วันนี้", text="ไฟล์วันนี้")),
                 QuickReplyButton(action=MessageAction(label="📅 ไฟล์เมื่อวาน", text="ไฟล์เมื่อวาน")),
                 QuickReplyButton(action=MessageAction(label="📂 ไฟล์ทั้งหมด", text="ไฟล์ทั้งหมด")),
                 QuickReplyButton(action=MessageAction(label="👤 ไฟล์ของฉัน", text="ไฟล์ของฉัน"))]
        line_bot_api.reply_message(reply_token, TextSendMessage(
            text="🔍 ค้นหาไฟล์ — เลือกประเภทที่ต้องการ:",
            quick_reply=QuickReply(items=items)
        ))
        return

    if text == "ไฟล์วันนี้":
        today = datetime.now(BKK).strftime("%Y-%m-%d")
        group_name = get_group_name(group_id)
        group_folder_id = get_or_create_folder(group_name, ROOT_FOLDER_ID)
        files = search_files_recursive(group_folder_id, date_str=today)
        reply_search_results(reply_token, "ไฟล์วันนี้", files)
        return

    if text == "ไฟล์เมื่อวาน":
        yesterday = (datetime.now(BKK) - timedelta(days=1)).strftime("%Y-%m-%d")
        group_name = get_group_name(group_id)
        group_folder_id = get_or_create_folder(group_name, ROOT_FOLDER_ID)
        files = search_files_recursive(group_folder_id, date_str=yesterday)
        reply_search_results(reply_token, "ไฟล์เมื่อวาน", files)
        return

    if text == "ไฟล์ทั้งหมด":
        group_name = get_group_name(group_id)
        group_folder_id = get_or_create_folder(group_name, ROOT_FOLDER_ID)
        folder_url = f"https://drive.google.com/drive/folders/{group_folder_id}"
        line_bot_api.reply_message(reply_token, TextSendMessage(
            text=f"📂 โฟลเดอร์กลุ่ม: {group_name}\n🔗 {folder_url}\n\nกดลิงก์เพื่อดูไฟล์ทั้งหมดใน Drive"
        ))
        return

    if text == "ไฟล์ของฉัน":
        sender_name = get_sender_name(user_id, group_id)
        group_name = get_group_name(group_id)
        group_folder_id = get_or_create_folder(group_name, ROOT_FOLDER_ID)
        sub_folders = get_subfolders(group_folder_id)
        files = []
        service = get_drive_service()
        for sf in sub_folders:
            sf_id_res = service.files().list(
                q=f"name='{sf}' and '{group_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id)"
            ).execute()
            if sf_id_res.get("files"):
                sf_id = sf_id_res["files"][0]["id"]
                res = service.files().list(
                    q=f"'{sf_id}' in parents and trashed=false and mimeType != 'application/vnd.google-apps.folder'",
                    fields="files(id, name, webViewLink, createdTime)",
                    pageSize=5
                ).execute()
                files.extend(res.get("files", []))
                if len(files) >= 5:
                    break
        reply_search_results(reply_token, f"ไฟล์ของ {sender_name}", files[:5])
        return

    if text == "/help" or text == "ช่วยเหลือ":
        line_bot_api.reply_message(reply_token, TextSendMessage(
            text="📁 File Bot — วิธีใช้\n\n• ส่งไฟล์/รูป/วิดีโอ → บอทถามโฟลเดอร์\n• พิมพ์ ค้นหา → เมนูค้นหาไฟล์\n\nฟรี 100% 🎉",
            quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔍 ค้นหา", text="ค้นหา"))])
        ))

# ===================================================
# File Handler
# ===================================================
def handle_file_message(event, file_type):
    user_id = event.source.user_id
    group_id = getattr(event.source, "group_id", None)
    push_to = group_id or user_id
    reply_token = event.reply_token

    sender_name = get_sender_name(user_id, group_id)
    sender_short = f"{sender_name} ({user_id[-6:]})"
    group_name = get_group_name(group_id)

    msg = event.message
    if file_type == "file":
        file_name = getattr(msg, "file_name", None) or f"file_{datetime.now(BKK).strftime('%Y%m%d_%H%M%S')}"
    elif file_type == "image":
        file_name = f"IMG_{datetime.now(BKK).strftime('%Y%m%d_%H%M%S')}.jpg"
    elif file_type == "video":
        file_name = f"VID_{datetime.now(BKK).strftime('%Y%m%d_%H%M%S')}.mp4"
    elif file_type == "audio":
        file_name = f"AUD_{datetime.now(BKK).strftime('%Y%m%d_%H%M%S')}.m4a"

    # เก็บ state
    pending_files[user_id] = {
        "messageId": msg.id,
        "fileName": file_name,
        "groupId": group_id,
        "groupName": group_name,
        "fileType": file_type,
        "senderShort": sender_short,
        "pushTo": push_to,
        "waitingForFolderName": False
    }

    # ดึงโฟลเดอร์ย่อยที่มีอยู่
    try:
        group_folder_id = get_or_create_folder(group_name, ROOT_FOLDER_ID)
        sub_folders = get_subfolders(group_folder_id)
    except:
        sub_folders = []

    items = []
    for sf in sub_folders[:10]:
        items.append(QuickReplyButton(action=MessageAction(label=f"📁 {sf[:20]}", text=f"📁 {sf}")))
    items.append(QuickReplyButton(action=MessageAction(label="➕ สร้างโฟลเดอร์ใหม่", text="➕ สร้างโฟลเดอร์ใหม่")))
    items.append(QuickReplyButton(action=MessageAction(label="📁 ทั่วไป", text="📁 ทั่วไป")))

    folder_list = f"โฟลเดอร์ที่มีอยู่: {', '.join(sub_folders[:3])}" if sub_folders else "ยังไม่มีโฟลเดอร์ย่อย"

    line_bot_api.reply_message(reply_token, TextSendMessage(
        text=f"📂 จะเก็บ \"{file_name}\" ไว้ในโฟลเดอร์ไหน?\n\n{folder_list}",
        quick_reply=QuickReply(items=items)
    ))

@handler.add(MessageEvent, message=FileMessage)
def handle_file(event):
    handle_file_message(event, "file")

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    handle_file_message(event, "image")

@handler.add(MessageEvent, message=VideoMessage)
def handle_video(event):
    handle_file_message(event, "video")

@handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    handle_file_message(event, "audio")

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
