import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def get_smtp_config(email_account: str):
    """根据邮箱后缀自动匹配最佳的 SMTP 服务器和端口"""
    email_account = email_account.lower().strip()
    if email_account.endswith("@gmail.com"):
        return "smtp.gmail.com", 587, "tls"
    elif email_account.endswith("@qq.com"):
        return "smtp.qq.com", 465, "ssl"
    elif email_account.endswith("@163.com"):
        return "smtp.163.com", 465, "ssl"
    else:
        domain = email_account.split("@")[-1]
        return f"smtp.{domain}", 587, "tls"

def send_email(subject: str, content: str):
    """
    发送邮件通知
    :param subject: 邮件主题
    :param content: 邮件正文 (支持 HTML / Plain)
    """
    account = os.environ.get("EMAIL_ACCOUNT", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    target_emails_str = os.environ.get("TARGET_EMAILS", "").strip()

    if not account or not password or not target_emails_str:
        print("❌ 邮件发送失败: EMAIL_ACCOUNT, EMAIL_PASSWORD 或 TARGET_EMAILS 环境变量未完整配置")
        return False

    # 支持逗号/分号分隔多个接收邮箱
    target_emails = [
        e.strip() for e in target_emails_str.replace(";", ",").split(",") if e.strip()
    ]

    if not target_emails:
        print("❌ 邮件发送失败: 接收邮箱列表为空")
        return False

    msg = MIMEMultipart()
    msg["From"] = account
    msg["To"] = ", ".join(target_emails)
    msg["Subject"] = subject

    # 判断并设置 HTML 格式
    if any(tag in content.lower() for tag in ["<html", "<div", "<p", "<table"]):
        msg.attach(MIMEText(content, "html", "utf-8"))
    else:
        msg.attach(MIMEText(content, "plain", "utf-8"))

    smtp_host, smtp_port, mode = get_smtp_config(account)

    # 执行发送（优先尝试默认端口，失败自动做容错处理）
    try:
        if mode == "ssl":
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20)
            server.login(account, password)
            server.sendmail(account, target_emails, msg.as_string())
            server.quit()
        else:  # Gmail 标准 TLS 模式
            try:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(account, password)
                server.sendmail(account, target_emails, msg.as_string())
                server.quit()
            except Exception as tls_err:
                print(f"⚠️ TLS 587 端口连接异常 ({tls_err})，正在尝试 465 SSL 备用模式...")
                server = smtplib.SMTP_SSL(smtp_host, 465, timeout=20)
                server.login(account, password)
                server.sendmail(account, target_emails, msg.as_string())
                server.quit()

        print(f"✅ 邮件已成功发送至: {', '.join(target_emails)}")
        return True

    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        return False
