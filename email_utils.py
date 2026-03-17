"""
Email utility functions for the Salla → Odoo webhook router.
"""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def send_welcome_email(
    merchant_name: str,
    merchant_email: str,
    merchant_id: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
    from_name: str = "FSolutions - Salla Integration"
) -> bool:
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'Welcome to Salla Webhook Integration Platform'
        msg['From'] = f'{from_name} <{from_email}>'
        msg['To'] = merchant_email
        
        # Plain text version
        text_body = f"""
Hello {merchant_name},

Welcome to the Salla Integration Platform by FSolutions

Your merchant account has been successfully created.

Account Details:
- Merchant ID: {merchant_id}
- Merchant Name: {merchant_name}

To complete the integration with your system and activate event synchronization,
please contact our team so we can finalize the setup and ensure everything is configured correctly for your store.

If you have any questions or need assistance, please don't hesitate to contact us at: +966530547274

Best regards,
FSolutions Team
+966530547274
Facilitating Solutions for Your Business

---
This is an automated message from the Salla Integration Platform.
"""
        
        # HTML version
        html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
        }}
        .header {{
            background: linear-gradient(135deg, #006233 0%, #0a9476 100%);
            color: white;
            padding: 30px;
            text-align: center;
            border-radius: 8px 8px 0 0;
        }}
        .header h1 {{
            margin: 0;
            font-size: 24px;
        }}
        .content {{
            background: #ffffff;
            padding: 30px;
            border: 1px solid #e0e0e0;
        }}
        .info-box {{
            background: #f8f9fa;
            border-left: 4px solid #0a9476;
            padding: 15px;
            margin: 20px 0;
        }}
        .info-box strong {{
            color: #006233;
        }}
        .features {{
            margin: 20px 0;
        }}
        .feature-item {{
            padding: 10px 0;
            border-bottom: 1px solid #e0e0e0;
        }}
        .feature-item:last-child {{
            border-bottom: none;
        }}
        .feature-item::before {{
            content: "✓ ";
            color: #0a9476;
            font-weight: bold;
            margin-right: 8px;
        }}
        .footer {{
            background: #f8f9fa;
            padding: 20px;
            text-align: center;
            font-size: 12px;
            color: #666;
            border-radius: 0 0 8px 8px;
        }}
        .cta-button {{
            display: inline-block;
            background: #0a9476;
            color: white;
            padding: 12px 30px;
            text-decoration: none;
            border-radius: 5px;
            margin: 20px 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Welcome to FSolutions - Salla Integration Platform</h1>
    </div>
    
    <div class="content">
        <p>Hello <strong>{merchant_name}</strong>,</p>
        
        <p>Welcome to the <strong>Salla Integration Platform</strong> by FSolutions</p>
        
        <p>Your merchant account has been successfully created.</p>
        
        <div class="info-box">
            <strong>Account Details:</strong><br>
            <strong>Merchant ID:</strong> {merchant_id}<br>
            <strong>Merchant Name:</strong> {merchant_name}
        </div>
        
        <p>To complete the integration with your system and activate event synchronization,
        please contact our team so we can finalize the setup and ensure everything is configured correctly for your store.</p>
        
        <p>If you have any questions or need assistance, please don't hesitate to contact us at: +966530547274</p>
        
        <p>Best regards,<br>
        <strong>FSolutions Team</strong><br>
        <strong>+966530547274</strong><br>
        <em>Facilitating Solutions for Your Business</em></p>
    </div>
    
    <div class="footer">
        <p>This is an automated message from the Salla Integration Platform.</p>
        <p>&copy; {2026} FSolutions. All rights reserved.</p>
    </div>
</body>
</html>
"""
        
        # Attach both versions
        part1 = MIMEText(text_body, 'plain')
        part2 = MIMEText(html_body, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        # Send email
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        
        logger.info(f"Welcome email sent to {merchant_email} for merchant {merchant_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send welcome email to {merchant_email}: {str(e)}")
        return False



def send_notification_email(
    merchant_name: str,
    merchant_email: str,
    merchant_id: str,
    odoo_url: str,
    phone_number: str,
    support_email: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
    from_name: str = "FSolutions - Salla Integration"
) -> bool:
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'New Merchant Installation: {merchant_name} ({merchant_id})'
        msg['From'] = f'{from_name} <{from_email}>'
        msg['To'] = support_email
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        
        text_body = f"""
Hi,

A new merchant has installed the Salla Integration app and a merchant 
record has been automatically created in the system.

Account Details:
- Merchant ID: {merchant_id}
- Merchant Name: {merchant_name}
- Odoo Webhook URL:  {odoo_url}
- Phone Number: {phone_number}
- Installation Time: {now}
- Status: NOT ACTIVATED (requires manual activation)

Webhooks will NOT be forwarded until manually activated
Login to admin panel: https://salla.fsodoo.org

Best regards,

---
This is an automated message from the Salla Integration Platform.
"""
        
        msg.attach(MIMEText(text_body, 'plain'))
        
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        
        logger.info(f"Notification email sent to {support_email} for merchant {merchant_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send notification email to {support_email}: {str(e)}")
        return False