# -*- coding: utf-8 -*-
"""
邮件发送，包括失败通知。

失败通知是这次架构讨论里明确提到、但旧代码完全没有的一层：原来任何
异常都是 print() 到 GitHub Actions 日志里，没人主动看就没人知道出过
问题——这次对话里好几个 bug 都是这样在数据里存在了不知道多久，直到
你自己手动发现异常才被追出来。这里加一个 notify_failure，任何主流程
里的未捕获异常都应该走这个函数，而不是只打印然后让整个 job 静默标红
（GitHub Actions 的红叉本身也不会主动推给你，除非你专门配置了通知）。
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def _get_email_targets() -> list:
    raw = os.environ.get("TARGET_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def send_email(subject: str, html_body: str) -> None:
    account = os.environ.get("EMAIL_ACCOUNT")
    password = os.environ.get("EMAIL_PASSWORD")
    targets = _get_email_targets()

    if not account or not password or not targets:
        print("⚠️ 邮件相关环境变量(EMAIL_ACCOUNT/EMAIL_PASSWORD/TARGET_EMAILS)未配置完整，跳过发送，仅打印内容。")
        print(f"[邮件主题] {subject}")
        return

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = account
    msg["To"] = ", ".join(targets)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20) as server:
            server.login(account, password)
            server.sendmail(account, targets, msg.as_string())
        print(f"✅ 邮件已发送至 {len(targets)} 个地址")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        raise


def notify_failure(context: str, error: Exception) -> None:
    """
    主流程崩溃时调用——发一封明确标注"失败"的邮件，而不是让异常只
    停留在 GitHub Actions 日志里等你自己发现。哪怕邮件本身也发送
    失败，这里也不会再抛出新的异常掩盖原始错误（用 try/except 包住
    发送本身，但原始异常在调用方那边应该继续往上抛，不在这里吞掉）。
    """
    subject = f"⚠️ 扫描任务失败: {context}"
    body = f"""
    <div style="font-family:sans-serif;padding:20px;">
        <h2 style="color:#c62828;">扫描任务执行失败</h2>
        <p><b>阶段:</b> {context}</p>
        <p><b>错误类型:</b> {type(error).__name__}</p>
        <p><b>错误信息:</b> {str(error)}</p>
        <p style="color:#888;font-size:13px;">请检查 GitHub Actions 运行日志获取完整堆栈信息。</p>
    </div>
    """
    try:
        send_email(subject, body)
    except Exception as notify_error:
        print(f"❌ 连失败通知邮件本身也发送失败了: {notify_error}（原始错误请看上面的日志）")
