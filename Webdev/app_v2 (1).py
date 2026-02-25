
import os
import re
import io
import json
import smtplib
from datetime import datetime, timedelta
from functools import wraps
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session, make_response, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from flask_mail import Mail, Message
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, SubmitField
from wtforms.validators import DataRequired, Email, ValidationError, Optional
from flask_apscheduler import APScheduler

# ReportLab for PDF Generation
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Load environment variables
load_dotenv(override=True)

# Initialize Flask app
app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///invoiceflow.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Mail configuration
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'welcome_page'
mail = Mail(app)

# Initialize Scheduler
scheduler = APScheduler()
scheduler.init_app(app)

# Prevent double execution of scheduler in debug mode
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    scheduler.start()

# ==================== HELPERS ====================

def generate_pdf_buffer(invoice):
    """Generates the PDF for an invoice and returns the byte buffer."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    left = 50
    right = width - 50
    y = height - 60

    # Header
    c.setFont("Helvetica-Bold", 18)
    c.drawString(left, y, "INVOICE")
    c.setFont("Helvetica", 12)
    c.drawRightString(right, y, invoice.invoice_number)

    y -= 22
    c.setFont("Helvetica", 10)
    c.drawString(left, y, f"Date: {invoice.date.strftime('%Y-%m-%d')}")
    c.drawString(left + 180, y, f"Due: {invoice.due_date.strftime('%Y-%m-%d')}")
    c.drawRightString(right, y, f"Status: {invoice.status.upper()}")

    # Bill to
    y -= 30
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "BILL TO")
    y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(left, y, invoice.client.name or "")
    y -= 12
    if invoice.client.email:
        c.drawString(left, y, invoice.client.email)
        y -= 12
    if invoice.client.phone:
        c.drawString(left, y, invoice.client.phone)
        y -= 12
    if invoice.client.address:
        for line in str(invoice.client.address).splitlines():
            c.drawString(left, y, line)
            y -= 12

    # Items table header
    y -= 18
    c.setFont("Helvetica-Bold", 10)
    c.drawString(left, y, "DESCRIPTION")
    c.drawRightString(left + 340, y, "QTY")
    c.drawRightString(left + 430, y, "UNIT")
    c.drawRightString(right, y, "AMOUNT")
    y -= 8
    c.line(left, y, right, y)
    y -= 14

    # Items rows
    c.setFont("Helvetica", 10)
    for item in invoice.items:
        if y < 120:
            c.showPage()
            y = height - 60
            c.setFont("Helvetica", 10)

        desc = (item.description or "")[:70]
        c.drawString(left, y, desc)
        c.drawRightString(left + 340, y, f"{item.quantity:g}")
        c.drawRightString(left + 430, y, f"{item.unit_price:.2f}")
        c.drawRightString(right, y, f"{item.amount:.2f}")
        y -= 14

    # Totals
    y -= 10
    c.line(left, y, right, y)
    y -= 18

    c.setFont("Helvetica", 10)
    c.drawRightString(right - 120, y, "Subtotal:")
    c.drawRightString(right, y, f"{invoice.subtotal:.2f}")
    y -= 14

    c.drawRightString(right - 120, y, f"Tax ({invoice.tax_rate:.2f}%):")
    c.drawRightString(right, y, f"{invoice.tax_amount:.2f}")
    y -= 14

    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(right - 120, y, "Total:")
    c.drawRightString(right, y, f"{invoice.total_amount:.2f}")
    y -= 24

    # Terms / Notes
    c.setFont("Helvetica-Bold", 10)
    if invoice.terms:
        c.drawString(left, y, "TERMS")
        y -= 12
        c.setFont("Helvetica", 10)
        for line in str(invoice.terms).splitlines():
            c.drawString(left, y, line[:95])
            y -= 12
        y -= 8

    if invoice.notes:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(left, y, "NOTES")
        y -= 12
        c.setFont("Helvetica", 10)
        for line in str(invoice.notes).splitlines():
            c.drawString(left, y, line[:95])
            y -= 12

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def send_email_direct(to_address, subject, html_body, attachment=None):
    """Sends an email using smtplib with robust attachment handling."""
    mail_user = app.config.get('MAIL_USERNAME')
    mail_pass = app.config.get('MAIL_PASSWORD')
    mail_server = app.config.get('MAIL_SERVER', 'smtp.gmail.com')
    mail_port = app.config.get('MAIL_PORT', 587)
    use_tls = app.config.get('MAIL_USE_TLS', True)

    if not mail_user or not mail_pass:
        print("Email skipped: MAIL_USERNAME or MAIL_PASSWORD not set")
        return False

    try:
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = mail_user
        msg['To'] = to_address

        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        if attachment:
            filename, file_bytes = attachment
            part = MIMEBase('application', 'pdf')
            part.set_payload(file_bytes.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
            msg.attach(part)
            print(f"📎 Attached PDF: {filename}")

        with smtplib.SMTP(mail_server, mail_port) as server:
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            server.login(mail_user, mail_pass)
            server.sendmail(mail_user, [to_address], msg.as_bytes())

        print(f"✅ Email sent successfully to {to_address}: {subject}")
        return True

    except smtplib.SMTPAuthenticationError:
        print("❌ Gmail Auth Error: Check your App Password.")
        return False
    except Exception as e:
        print(f"❌ Email Error: {str(e)}")
        return False


def send_invoice_email(invoice):
    """Send the full invoice contents to the client email, with PDF attachment."""
    mail_user = app.config.get('MAIL_USERNAME')
    mail_pass = app.config.get('MAIL_PASSWORD')
    if not mail_user or not mail_pass:
        print("Info: Invoice email skipped - mail not configured")
        return False

    client = invoice.client
    if not client or not client.email:
        print("Info: Invoice email skipped - no client email on invoice")
        return False

    # Generate PDF attachment
    try:
        pdf_buffer = generate_pdf_buffer(invoice)
        pdf_attachment = (f"{invoice.invoice_number}.pdf", pdf_buffer)
    except Exception as e:
        print(f"❌ PDF Generation failed: {e}")
        pdf_attachment = None

    try:
        items_rows = ""
        for item in invoice.items:
            items_rows += (
                "<tr>"
                "<td style='padding:10px 12px;border-bottom:1px solid #e8ecf0;'>" + str(item.description) + "</td>"
                "<td style='padding:10px 12px;border-bottom:1px solid #e8ecf0;text-align:center;'>" + "{:g}".format(item.quantity) + "</td>"
                "<td style='padding:10px 12px;border-bottom:1px solid #e8ecf0;text-align:right;'>${:.2f}".format(item.unit_price) + "</td>"
                "<td style='padding:10px 12px;border-bottom:1px solid #e8ecf0;text-align:right;font-weight:600;'>${:.2f}".format(item.amount) + "</td>"
                "</tr>"
            )

        title_line = ("<p style='margin:0 0 4px 0;font-size:15px;color:#d0e0ff;'>" + invoice.title + "</p>") if invoice.title else ""
        client_email_line = ("<p style='margin:2px 0 0;color:#666;font-size:13px;'>" + client.email + "</p>") if client.email else ""
        client_phone_line = ("<p style='margin:2px 0 0;color:#666;font-size:13px;'>" + client.phone + "</p>") if client.phone else ""

        notes_section = ""
        if invoice.notes:
            notes_section = (
                "<div style='margin-top:24px;padding:16px;background:#f8f9fa;border-radius:6px;'>"
                "<p style='margin:0 0 6px;font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.5px;'>Notes</p>"
                "<p style='margin:0;color:#444;font-size:14px;'>" + invoice.notes + "</p>"
                "</div>"
            )

        terms_section = ""
        if invoice.terms:
            terms_section = (
                "<div style='margin-top:16px;padding:16px;background:#f8f9fa;border-radius:6px;'>"
                "<p style='margin:0 0 6px;font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.5px;'>Terms &amp; Conditions</p>"
                "<p style='margin:0;color:#444;font-size:14px;'>" + invoice.terms + "</p>"
                "</div>"
            )

        html_body = (
            "<html><body style='font-family:Arial,sans-serif;line-height:1.6;color:#333;background:#f0f2f5;margin:0;padding:0;'>"
            "<div style='max-width:640px;margin:32px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);'>"

            "<div style='background:#4a6fa5;padding:28px 32px;'>"
            "<h1 style='margin:0;color:#fff;font-size:22px;font-weight:700;'>Invoice " + invoice.invoice_number + "</h1>"
            + title_line +
            "</div>"

            "<div style='padding:28px 32px;'>"

            "<table style='width:100%;margin-bottom:28px;'><tr>"
            "<td style='width:50%;vertical-align:top;padding:16px;background:#f8f9fa;border-radius:6px;'>"
            "<p style='margin:0 0 4px;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;'>Bill To</p>"
            "<p style='margin:0;font-weight:600;font-size:15px;'>" + client.name + "</p>"
            + client_email_line + client_phone_line +
            "</td>"
            "<td style='width:8px;'></td>"
            "<td style='width:50%;vertical-align:top;padding:16px;background:#f8f9fa;border-radius:6px;'>"
            "<p style='margin:0 0 8px;'><span style='font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;'>Invoice Date</span><br><strong>" + invoice.date.strftime("%d %b %Y") + "</strong></p>"
            "<p style='margin:0;'><span style='font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;'>Due Date</span><br><strong>" + invoice.due_date.strftime("%d %b %Y") + "</strong></p>"
            "</td>"
            "</tr></table>"

            "<table style='width:100%;border-collapse:collapse;margin-bottom:20px;'>"
            "<thead><tr style='background:#f8f9fa;'>"
            "<th style='padding:10px 12px;text-align:left;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #e8ecf0;'>Description</th>"
            "<th style='padding:10px 12px;text-align:center;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #e8ecf0;'>Qty</th>"
            "<th style='padding:10px 12px;text-align:right;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #e8ecf0;'>Unit Price</th>"
            "<th style='padding:10px 12px;text-align:right;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #e8ecf0;'>Amount</th>"
            "</tr></thead>"
            "<tbody>" + items_rows + "</tbody>"
            "</table>"

            "<table style='margin-left:auto;width:260px;margin-bottom:24px;'>"
            "<tr><td style='padding:8px 0;border-bottom:1px solid #e8ecf0;font-size:14px;color:#666;'>Subtotal</td>"
            "<td style='padding:8px 0;border-bottom:1px solid #e8ecf0;font-size:14px;text-align:right;'>${:.2f}".format(invoice.subtotal) + "</td></tr>"
            "<tr><td style='padding:8px 0;border-bottom:1px solid #e8ecf0;font-size:14px;color:#666;'>Tax (" + str(invoice.tax_rate) + "%)</td>"
            "<td style='padding:8px 0;border-bottom:1px solid #e8ecf0;font-size:14px;text-align:right;'>${:.2f}".format(invoice.tax_amount) + "</td></tr>"
            "<tr><td style='padding:10px 0;font-size:16px;font-weight:700;'>Total</td>"
            "<td style='padding:10px 0;font-size:16px;font-weight:700;text-align:right;color:#4a6fa5;'>${:.2f}".format(invoice.total_amount) + "</td></tr>"
            "</table>"

            + notes_section + terms_section +

            "</div>"

            "<div style='padding:20px 32px;background:#f8f9fa;border-top:1px solid #e8ecf0;'>"
            "<p style='margin:0;font-size:12px;color:#666;'>"
            "<strong>InvoiceFlow</strong><br>"
            "This is an automated notification from your invoice management system."
            "</p></div>"

            "</div></body></html>"
        )

        return send_email_direct(
            client.email,
            "Invoice " + invoice.invoice_number + " from InvoiceFlow",
            html_body,
            attachment=pdf_attachment
        )

    except Exception as e:
        print("Failed to send invoice email: " + str(e))
        return False


def process_recurring_invoices():
    """Scheduler Job: Checks for recurring invoices due to be sent."""
    with app.app_context():
        now = datetime.utcnow()
        recurring_templates = Invoice.query.filter(
            Invoice.is_recurring == True,
            Invoice.next_send_date <= now,
            Invoice.status != 'cancelled'
        ).all()

        if recurring_templates:
            print(f"⏰ Scheduler: Processing {len(recurring_templates)} recurring invoices...")

        for template in recurring_templates:
            # 1. Generate New Invoice Number
            new_number = generate_invoice_number(template.user, template.client)

            # 2. Create the Child Invoice (Start as DRAFT)
            new_invoice = Invoice(
                invoice_number=new_number,
                date=now,
                due_date=now + (template.due_date - template.date),
                tax_rate=template.tax_rate,
                notes=template.notes,
                terms=template.terms,
                user_id=template.user_id,
                client_id=template.client_id,
                title=template.title,
                status='draft',
                is_recurring=False,
                frequency=None,
                initial_send_date=None,
                next_send_date=None
            )
            db.session.add(new_invoice)
            db.session.flush()

            # 3. Copy Items
            for item in template.items:
                db.session.add(InvoiceItem(
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    amount=item.amount,
                    invoice_id=new_invoice.id
                ))

            new_invoice.calculate_totals()

            # 4. Update Template's Next Send Date
            template.next_send_date = template.calculate_next_send_date()

            db.session.commit()

            # 5. Send Email with PDF & Update Status ONLY on Success
            print(f"🚀 Attempting to send recurring invoice {new_invoice.invoice_number}...")
            if send_invoice_email(new_invoice):
                new_invoice.status = 'sent'
                db.session.commit()
                print(f"✅ Invoice {new_invoice.invoice_number} sent and status updated to 'sent'.")

                create_notification(
                    template.user_id,
                    "Recurring Invoice Sent",
                    f"Automated invoice {new_invoice.invoice_number} successfully sent to {template.client.name}",
                    send_email=False
                )
            else:
                print(f"❌ Failed to send invoice {new_invoice.invoice_number}. Status remains 'draft'.")
                create_notification(
                    template.user_id,
                    "Recurring Email Failed",
                    f"Generated invoice {new_invoice.invoice_number} but email failed. Status is Draft.",
                    send_email=False
                )


# ==================== MODELS ====================

# Database Models
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    company_name = db.Column(db.String(100), default='My Company')
    invoice_prefix = db.Column(db.String(20), default='INV')
    role = db.Column(db.String(20), default='customer')
    email_notifications_enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_admin(self):
        return self.role == 'admin'


class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    address = db.Column(db.Text)
    invoice_prefix = db.Column(db.String(20))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    invoices = db.relationship('Invoice', backref='client', lazy=True)


class Invoice(db.Model):
    __tablename__ = 'invoices'
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    due_date = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='draft')
    total_amount = db.Column(db.Float, default=0.0)
    tax_rate = db.Column(db.Float, default=0.0)
    tax_amount = db.Column(db.Float, default=0.0)
    subtotal = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text)
    terms = db.Column(db.Text)
    title = db.Column(db.String(200))
    reminder_sent = db.Column(db.Boolean, default=False)

    # Recurring Fields
    is_recurring = db.Column(db.Boolean, default=False)
    frequency = db.Column(db.String(20))
    initial_send_date = db.Column(db.DateTime)
    next_send_date = db.Column(db.DateTime)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    items = db.relationship('InvoiceItem', backref='invoice', lazy=True, cascade='all, delete-orphan')

    def calculate_totals(self):
        self.subtotal = sum(item.amount for item in self.items)
        self.tax_amount = self.subtotal * (self.tax_rate / 100)
        self.total_amount = self.subtotal + self.tax_amount

    def is_completed(self):
        return self.status in ['paid', 'cancelled']

    def calculate_next_send_date(self):
        if not self.is_recurring or not self.frequency:
            return None
        base_date = self.next_send_date or self.initial_send_date or self.date
        if self.frequency == 'daily':
            return base_date + timedelta(days=1)
        elif self.frequency == 'weekly':
            return base_date + timedelta(weeks=1)
        elif self.frequency == 'monthly':
            return base_date + timedelta(days=30)
        return None


class InvoiceItem(db.Model):
    __tablename__ = 'invoice_items'
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    unit_price = db.Column(db.Float, default=0.0)
    amount = db.Column(db.Float, default=0.0)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=False)

    def calculate_amount(self):
        self.amount = self.quantity * self.unit_price


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    details = db.Column(db.Text)


class Notification(db.Model):
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ==================== FORMS & VALIDATORS ====================

# Custom Validators
def validate_singapore_phone(form, field):
    if field.data:
        phone = re.sub(r'[^0-9+]', '', field.data)
        patterns = [r'^[89]\d{7}$', r'^6\d{7}$', r'^\+65[689]\d{7}$']
        if not any(re.match(pattern, phone) for pattern in patterns):
            raise ValidationError('Please enter a valid Singapore phone number (e.g., 91234567 or +6591234567)')


class ClientForm(FlaskForm):
    name = StringField('Client Name', validators=[DataRequired(message='Client name is required')])
    email = StringField('Email', validators=[Optional(), Email(message='Please enter a valid email address')])
    phone = StringField('Phone Number', validators=[Optional(), validate_singapore_phone])
    address = TextAreaField('Address', validators=[Optional()])
    invoice_prefix = StringField('Invoice Prefix', validators=[Optional()])
    submit = SubmitField('Save Client')


# Add template filters and context processors
@app.context_processor
def utility_processor():
    return dict(datetime=datetime, timedelta=timedelta, now=datetime.utcnow())


@app.template_filter('format_currency')
def format_currency(value):
    return "$0.00" if value is None else "${:,.2f}".format(value)


@app.template_filter('to_sg_time')
def to_sg_time(utc_datetime):
    """Convert UTC datetime to Singapore time (UTC+8)"""
    if not utc_datetime:
        return ""
    sg_time = utc_datetime + timedelta(hours=8)
    return sg_time.strftime('%B %d, %Y at %I:%M %p')


# Login manager callback
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Helper functions
def generate_invoice_number(user=None, client=None, prefix="INV"):
    if client and client.invoice_prefix:
        prefix = client.invoice_prefix.upper()
    elif user and user.invoice_prefix:
        prefix = user.invoice_prefix.upper()
    else:
        prefix = prefix.upper()

    today = datetime.now()
    year_month = today.strftime('%Y%m')
    pattern = f'{prefix}-{year_month}-%'

    last_invoice = Invoice.query.filter(
        Invoice.invoice_number.like(pattern)
    ).order_by(Invoice.invoice_number.desc()).first()

    if last_invoice:
        last_num = int(last_invoice.invoice_number.split('-')[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f'{prefix}-{year_month}-{new_num:04d}'


def log_audit(action, entity_type, entity_id, details=""):
    if current_user.is_authenticated:
        log = AuditLog(
            user_id=current_user.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details
        )
        db.session.add(log)
        db.session.commit()


def create_notification(user_id, title, message, send_email=True):
    """Create in-app notification and optionally send email"""
    try:
        notification = Notification(
            user_id=user_id,
            title=title,
            message=message
        )
        db.session.add(notification)
        db.session.commit()
        print(f"✓ In-app notification created: {title}")

        # Send email notification only if mail is properly configured
        mail_user = app.config.get('MAIL_USERNAME')
        mail_pass = app.config.get('MAIL_PASSWORD')
        mail_configured = bool(mail_user and mail_pass)

        if send_email and mail_configured:
            user = User.query.get(user_id)
            if user and user.email:
                try:
                    html_body = (
                        "<html><body style='font-family:Arial,sans-serif;line-height:1.6;color:#333;background:#f0f2f5;margin:0;padding:0;'>"
                        "<div style='max-width:640px;margin:32px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);'>"
                        "<div style='background:#4a6fa5;padding:28px 32px;'>"
                        "<h1 style='margin:0;color:#fff;font-size:22px;font-weight:700;'>" + title + "</h1>"
                        "</div>"
                        "<div style='padding:28px 32px;'>"
                        "<p style='font-size:15px;color:#444;'>" + message + "</p>"
                        "</div>"
                        "<div style='padding:20px 32px;background:#f8f9fa;border-top:1px solid #e8ecf0;'>"
                        "<p style='margin:0;font-size:12px;color:#666;'><strong>InvoiceFlow</strong><br>"
                        "This is an automated notification from your invoice management system.<br>"
                        "<a href='http://localhost:5000/notifications' style='color:#4a6fa5;text-decoration:none;'>View all notifications &rarr;</a>"
                        "</p></div>"
                        "</div></body></html>"
                    )
                    send_email_direct(user.email, f"InvoiceFlow Notification: {title}", html_body)
                    print(f"✓ Email notification sent to {user.email}")
                except Exception as e:
                    print(f"✗ Failed to send email to {user.email}: {str(e)}")
        elif send_email and not mail_configured:
            print(f"ℹ Email skipped — MAIL_USERNAME: {repr(mail_user)}, MAIL_PASSWORD set: {bool(mail_pass)}")

        return True
    except Exception as e:
        print(f"✗ Failed to create notification: {str(e)}")
        db.session.rollback()
        return False


# Role-based access control decorators
def user_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin():
            flash('Admin access required. You have been redirected to your invoices.', 'error')
            return redirect(url_for('customer_invoices_page'))
        return f(*args, **kwargs)
    return decorated_function


# ==================== ROUTES ====================

# Welcome and User Type Selection Routes
@app.route('/')
@app.route('/welcome')
def welcome_page():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    session.pop('user_type', None)
    return render_template('welcome.html')


@app.route('/set-user-type/<user_type>')
def set_user_type(user_type):
    if user_type in ['admin', 'customer']:
        session['user_type'] = user_type
        return redirect(url_for('login_page'))
    else:
        flash('Invalid user type', 'error')
        return redirect(url_for('welcome_page'))


# Login Route
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('invoices_page'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user_type = request.form.get('user_type', session.get('user_type', ''))

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            if user_type and user.role != user_type:
                flash(f'Please login as a {user_type}', 'error')
                return redirect(url_for('login_page'))

            login_user(user)
            session.pop('user_type', None)
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('invoices_page'))
        else:
            flash('Invalid username or password', 'error')

    return render_template('login.html')


@app.route('/logout')
@user_required
def logout_page():
    logout_user()
    session.clear()
    flash('You have been logged out successfully', 'success')
    return redirect(url_for('welcome_page'))


# ==================== INVOICE ROUTES ====================

@app.route('/dashboard')
@user_required
def dashboard():
    if not current_user.is_admin():
        return redirect(url_for('customer_invoices_page'))
    total_invoices = Invoice.query.filter_by(user_id=current_user.id).count()
    draft_count = Invoice.query.filter_by(user_id=current_user.id, status='draft').count()
    sent_count = Invoice.query.filter_by(user_id=current_user.id, status='sent').count()
    paid_count = Invoice.query.filter_by(user_id=current_user.id, status='paid').count()
    recent_invoices = Invoice.query.filter_by(user_id=current_user.id).order_by(Invoice.date.desc()).limit(5).all()
    recent_clients = Client.query.filter_by(user_id=current_user.id).order_by(Client.id.desc()).limit(5).all()
    return render_template('index.html',
                           total_invoices=total_invoices,
                           draft_count=draft_count,
                           sent_count=sent_count,
                           paid_count=paid_count,
                           recent_invoices=recent_invoices,
                           recent_clients=recent_clients)


@app.route('/invoices')
@admin_required
def invoices_page():
    status_filter = request.args.get('status', 'all')
    client_filter = request.args.get('client_id', type=int)

    query = Invoice.query.filter_by(user_id=current_user.id).filter(Invoice.status.notin_(['paid', 'cancelled']))

    if status_filter != 'all':
        query = query.filter_by(status=status_filter)

    if client_filter:
        query = query.filter_by(client_id=client_filter)

    invoices = query.order_by(Invoice.date.desc()).all()
    clients = Client.query.filter_by(user_id=current_user.id).all()

    return render_template('view_invoices.html',
                           invoices=invoices,
                           clients=clients,
                           status_filter=status_filter,
                           client_filter=client_filter)


@app.route('/invoices/completed')
@admin_required
def completed_invoices_page():
    status_filter = request.args.get('status', 'all')
    client_filter = request.args.get('client_id', type=int)

    query = Invoice.query.filter_by(user_id=current_user.id)

    if status_filter == 'all':
        query = query.filter(Invoice.status.in_(['paid', 'cancelled']))
    else:
        query = query.filter_by(status=status_filter)

    if client_filter:
        query = query.filter_by(client_id=client_filter)

    invoices = query.order_by(Invoice.date.desc()).all()
    clients = Client.query.filter_by(user_id=current_user.id).all()

    return render_template('completed_invoices.html',
                           invoices=invoices,
                           clients=clients,
                           status_filter=status_filter,
                           client_filter=client_filter)


@app.route('/invoices/create', methods=['GET', 'POST'])
@admin_required
def create_invoice_page():
    clients = Client.query.filter_by(user_id=current_user.id).all()

    if request.method == 'POST':
        try:
            # Get form data
            title = request.form.get('title', '')
            client_id = request.form.get('client_id')
            is_recurring = request.form.get('is_recurring') == 'on'
            frequency = request.form.get('frequency')

            due_date_str = request.form.get('due_date')
            due_time_str = request.form.get('due_time', '17:00')
            tax_rate = float(request.form.get('tax_rate', 0) or 0)
            notes = request.form.get('notes', '').strip()
            terms = request.form.get('terms', '').strip()

            # Invoice prefix
            invoice_prefix = (request.form.get('invoice_prefix') or '').strip().upper()

            # Items
            item_descriptions = request.form.getlist('item_description[]')
            item_quantities = request.form.getlist('item_quantity[]')
            item_prices = request.form.getlist('item_price[]')

            # Basic validation
            if not client_id:
                flash('Client is required', 'error')
                return redirect(url_for('create_invoice_page'))

            if not due_date_str:
                flash('Due date is required', 'error')
                return redirect(url_for('create_invoice_page'))

            # Ensure at least one non-empty description
            if not any(d.strip() for d in item_descriptions):
                flash('At least one invoice item is required', 'error')
                return redirect(url_for('create_invoice_page'))

            client = Client.query.filter_by(id=client_id, user_id=current_user.id).first_or_404()

            # Parse dates
            invoice_date = datetime.utcnow()
            due_datetime = datetime.strptime(f"{due_date_str} {due_time_str}", "%Y-%m-%d %H:%M")

            # Generate invoice number
            invoice_number = generate_invoice_number(current_user, client, invoice_prefix or 'INV')

            # Recurring Logic Setup
            initial_send = None
            next_send = None
            if is_recurring:
                initial_send = datetime.utcnow()
                if frequency == 'daily':
                    next_send = initial_send + timedelta(days=1)
                elif frequency == 'weekly':
                    next_send = initial_send + timedelta(weeks=1)
                elif frequency == 'monthly':
                    next_send = initial_send + timedelta(days=30)

            # Create invoice
            invoice = Invoice(
                invoice_number=invoice_number,
                date=invoice_date,
                due_date=due_datetime,
                tax_rate=tax_rate,
                notes=notes,
                terms=terms,
                user_id=current_user.id,
                client_id=int(client_id),
                title=title,
                is_recurring=is_recurring,
                frequency=frequency,
                initial_send_date=initial_send,
                next_send_date=next_send
            )
            db.session.add(invoice)

            db.session.flush()  # get invoice.id before adding items

            # Add items (only add rows with a description)
            for desc, qty_str, price_str in zip(item_descriptions, item_quantities, item_prices):
                desc = (desc or '').strip()
                if not desc:
                    continue

                quantity = float(qty_str or 0)
                unit_price = float(price_str or 0)
                amount = quantity * unit_price

                db.session.add(InvoiceItem(
                    description=desc,
                    quantity=quantity,
                    unit_price=unit_price,
                    amount=amount,
                    invoice_id=invoice.id
                ))

            # Totals + save
            invoice.calculate_totals()
            db.session.commit()

            create_notification(
                current_user.id,
                "Invoice Created",
                f"Invoice {invoice.invoice_number} has been created (Draft)",
                send_email=False
            )
            log_audit('create', 'invoice', invoice.id, f'Created invoice {invoice.invoice_number}')

            # Send invoice email — mark sent only if email succeeds
            if not is_recurring:
                if send_invoice_email(invoice):
                    invoice.status = 'sent'
                    db.session.commit()
                    flash(f'Invoice {invoice.invoice_number} created and email sent!', 'success')
                else:
                    flash(f'Invoice {invoice.invoice_number} created but email FAILED. Status: Draft.', 'warning')
            else:
                if send_invoice_email(invoice):
                    invoice.status = 'sent'
                    db.session.commit()
                    flash(f'Recurring Invoice {invoice.invoice_number} started and first email sent!', 'success')
                else:
                    flash(f'Recurring Invoice created but first email FAILED. Status: Draft.', 'warning')

            return redirect(url_for('invoice_detail_page', invoice_id=invoice.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Error creating invoice: {str(e)}', 'error')

    return render_template('create_invoice.html', clients=clients, today=datetime.utcnow())


@app.route('/invoices/<int:invoice_id>')
@user_required
def invoice_detail_page(invoice_id):
    if current_user.is_admin():
        invoice = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first_or_404()
    else:
        invoice = Invoice.query.join(Client).filter(
            Invoice.id == invoice_id,
            Client.email == current_user.email
        ).first_or_404()
    return render_template('invoice_detail.html', invoice=invoice)


@app.route('/invoices/<int:invoice_id>/update-number', methods=['POST'])
@admin_required
def update_invoice_number(invoice_id):
    invoice = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first_or_404()
    new_number = request.form.get('invoice_number', '').strip()

    if not new_number:
        flash('Invoice number cannot be empty', 'error')
        return redirect(url_for('invoice_detail_page', invoice_id=invoice_id))

    # Check duplicate
    if Invoice.query.filter(Invoice.invoice_number == new_number, Invoice.id != invoice_id).first():
        flash(f'Invoice number "{new_number}" already exists', 'error')
        return redirect(url_for('invoice_detail_page', invoice_id=invoice_id))

    old_number = invoice.invoice_number
    invoice.invoice_number = new_number
    db.session.commit()

    log_audit('update', 'invoice', invoice.id, f'Invoice number changed from {old_number} to {new_number}')
    flash(f'Invoice number updated to {new_number}', 'success')
    return redirect(url_for('invoice_detail_page', invoice_id=invoice_id))


@app.route('/invoices/<int:invoice_id>/update-status', methods=['POST'])
@admin_required
def update_invoice_status(invoice_id):
    invoice = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first_or_404()
    new_status = request.form.get('status')

    if new_status in ['draft', 'sent', 'paid', 'overdue', 'cancelled']:
        old_status = invoice.status

        # If marking as 'sent', verify email succeeds first
        if new_status == 'sent':
            if send_invoice_email(invoice):
                invoice.status = 'sent'
                db.session.commit()
                flash('Status updated to Sent and email delivered.', 'success')
            else:
                flash('Failed to send email. Invoice status NOT changed to Sent.', 'error')
                return redirect(url_for('invoice_detail_page', invoice_id=invoice_id))
        else:
            invoice.status = new_status
            db.session.commit()

            status_messages = {
                'draft': 'moved to Draft',
                'paid': 'has been Paid ✓',
                'overdue': 'is now Overdue ⚠️',
                'cancelled': 'has been Cancelled'
            }

            notification_message = f"Invoice {invoice.invoice_number} {status_messages.get(new_status, new_status)}"
            create_notification(
                current_user.id,
                f"Invoice Status Changed: {new_status.capitalize()}",
                notification_message
            )

            log_audit('update', 'invoice', invoice.id, f'Status changed from {old_status} to {new_status}')
            flash(f'Invoice status updated to {new_status}', 'success')

    return redirect(url_for('invoice_detail_page', invoice_id=invoice_id))


@app.route('/invoices/<int:invoice_id>/delete', methods=['POST'])
@admin_required
def delete_invoice(invoice_id):
    invoice = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first_or_404()
    invoice_number = invoice.invoice_number

    db.session.delete(invoice)
    db.session.commit()

    log_audit('delete', 'invoice', invoice_id, f'Deleted invoice {invoice_number}')
    flash(f'Invoice {invoice_number} deleted successfully', 'success')
    return redirect(url_for('invoices_page'))


@app.route('/invoices/<int:invoice_id>/pdf')
@user_required
def download_invoice_pdf(invoice_id):
    if current_user.is_admin():
        invoice = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first_or_404()
    else:
        invoice = Invoice.query.join(Client).filter(
            Invoice.id == invoice_id,
            Client.email == current_user.email
        ).first_or_404()
    buffer = generate_pdf_buffer(invoice)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{invoice.invoice_number}.pdf",
        mimetype="application/pdf",
    )


# ==================== CLIENT ROUTES ====================

@app.route('/clients')
@admin_required
def clients_page():
    clients = Client.query.filter_by(user_id=current_user.id).order_by(Client.name).all()
    return render_template('clients_list.html', clients=clients)


@app.route('/clients/create', methods=['GET', 'POST'])
@admin_required
def create_client_page():
    form = ClientForm()
    if form.validate_on_submit():
        try:
            client = Client(
                name=form.name.data,
                email=form.email.data,
                phone=form.phone.data,
                address=form.address.data,
                invoice_prefix=form.invoice_prefix.data,
                user_id=current_user.id
            )
            db.session.add(client)
            db.session.commit()
            log_audit('create', 'client', client.id, f'Created client {client.name}')
            flash(f'Client {client.name} created successfully', 'success')
            return redirect(url_for('clients_page'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating client: {str(e)}', 'error')
    return render_template('create_client.html', form=form)


@app.route('/clients/<int:client_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_client_page(client_id):
    client = Client.query.filter_by(id=client_id, user_id=current_user.id).first_or_404()
    form = ClientForm(obj=client)
    if form.validate_on_submit():
        try:
            client.name = form.name.data
            client.email = form.email.data
            client.phone = form.phone.data
            client.address = form.address.data
            client.invoice_prefix = form.invoice_prefix.data
            db.session.commit()

            log_audit('update', 'client', client.id, f'Updated client {client.name}')
            flash(f'Client {client.name} updated successfully', 'success')
            return redirect(url_for('clients_page'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating client: {str(e)}', 'error')

    return render_template('edit_client.html', form=form, client=client)


@app.route('/clients/<int:client_id>/delete', methods=['POST'])
@admin_required
def delete_client(client_id):
    client = Client.query.filter_by(id=client_id, user_id=current_user.id).first_or_404()
    if client.invoices:
        flash(f'Cannot delete {client.name} - client has existing invoices', 'error')
        return redirect(url_for('clients_page'))

    client_name = client.name
    db.session.delete(client)
    db.session.commit()

    log_audit('delete', 'client', client_id, f'Deleted client {client_name}')
    flash(f'Client {client_name} deleted successfully', 'success')
    return redirect(url_for('clients_page'))




# ==================== CUSTOMER ROUTES ====================

@app.route('/my-invoices')
@user_required
def customer_invoices_page():
    """Customer portal: view invoices addressed to them by matching email."""
    if current_user.is_admin():
        return redirect(url_for('invoices_page'))

    status_filter = request.args.get('status', 'all')
    
    # Base query for customer's invoices - only show paid and overdue
    query = Invoice.query.join(Client).filter(
        Client.email == current_user.email,
        Invoice.status.in_(['paid', 'overdue'])  # Only show paid and overdue
    )
    
    # Apply status filter if not 'all'
    if status_filter != 'all':
        query = query.filter(Invoice.status == status_filter)
    
    # Get all invoices for the current view (filtered)
    all_invoices = query.order_by(Invoice.date.desc()).all()
    
    # Get only the latest 5 invoices for display
    latest_invoices = all_invoices[:5] if all_invoices else []
    
    # Calculate total due from all relevant invoices (not just latest)
    total_due = sum(inv.total_amount for inv in all_invoices if inv.status in ['overdue'])
    
    return render_template('customer_invoices.html',
                           invoices=latest_invoices,
                           all_invoices_count=len(all_invoices),
                           status_filter=status_filter,
                           total_due=total_due)

@app.route('/my-invoices/<int:invoice_id>')
@user_required
def customer_invoice_detail_page(invoice_id):
    """Customer portal: view a specific invoice."""
    if current_user.is_admin():
        return redirect(url_for('invoice_detail_page', invoice_id=invoice_id))
    
    invoice = Invoice.query.join(Client).filter(
        Invoice.id == invoice_id,
        Client.email == current_user.email
    ).first_or_404()
    
    return render_template('customer_invoice_detail.html', invoice=invoice)

@app.route('/my-invoices/<int:invoice_id>/pay', methods=['POST'])
@user_required
def customer_pay_invoice(invoice_id):
    """Customer action: mark their own invoice as paid."""
    if current_user.is_admin():
        flash('Please use the admin panel to update invoice status.', 'info')
        return redirect(url_for('invoice_detail_page', invoice_id=invoice_id))

    invoice = Invoice.query.join(Client).filter(
        Invoice.id == invoice_id,
        Client.email == current_user.email
    ).first_or_404()

    if invoice.status in ['paid', 'cancelled']:
        flash(f'Invoice {invoice.invoice_number} is already {invoice.status}.', 'info')
    else:
        invoice.status = 'paid'
        db.session.commit()
        
        # Log audit
        log_audit('update', 'invoice', invoice.id, f'Customer marked invoice {invoice.invoice_number} as paid')
        
        # Get the client name from the invoice
        client_name = invoice.client.name
        
        # Send notification to ADMIN (invoice creator)
        create_notification(
            invoice.user_id,
            '💰 Invoice Paid by Customer',
            f'{client_name} has marked invoice {invoice.invoice_number} as paid. Amount: ${invoice.total_amount:.2f}',
            send_email=True
        )
        
        # Send notification to CUSTOMER (the one who just paid)
        create_notification(
            current_user.id,
            '✅ Payment Confirmation',
            f'Thank you, {client_name}! Your payment for invoice {invoice.invoice_number} in the amount of ${invoice.total_amount:.2f} has been confirmed.',
            send_email=True
        )
        
        flash(f'Invoice {invoice.invoice_number} marked as paid. Thank you!', 'success')

    return redirect(url_for('customer_invoices_page'))

@app.route('/my-invoices/all')
@user_required
def customer_all_invoices_page():
    """Customer portal: view all invoice records."""
    if current_user.is_admin():
        return redirect(url_for('invoices_page'))

    status_filter = request.args.get('status', 'all')
    
    # Base query for customer's invoices - only show paid and overdue
    query = Invoice.query.join(Client).filter(
        Client.email == current_user.email,
        Invoice.status.in_(['paid', 'overdue'])  # Only show paid and overdue
    )
    
    # Apply status filter if not 'all'
    if status_filter != 'all':
        query = query.filter(Invoice.status == status_filter)
    
    # Get all invoices ordered by date
    invoices = query.order_by(Invoice.date.desc()).all()
    
    # Calculate totals
    total_due = sum(inv.total_amount for inv in invoices if inv.status in ['overdue'])
    total_paid = sum(inv.total_amount for inv in invoices if inv.status == 'paid')
    total_overdue = sum(inv.total_amount for inv in invoices if inv.status == 'overdue')
    
    return render_template('customer_all_invoices.html',
                           invoices=invoices,
                           status_filter=status_filter,
                           total_due=total_due,
                           total_paid=total_paid,
                           total_overdue=total_overdue)
# ==================== SETTINGS ROUTES ====================

@app.route('/settings')
@user_required
def settings_page():
    return render_template('settings.html', user=current_user)


@app.route('/settings/update', methods=['POST'])
@user_required
def update_settings():
    # Check if email is being changed and if it's already taken
    new_email = request.form.get('email', current_user.email)
    
    # If email is being changed, check if it's already used by another user
    if new_email != current_user.email:
        existing_user = User.query.filter_by(email=new_email).first()
        if existing_user:
            flash('This email address is already registered to another account. Please use a different email.', 'error')
            return redirect(url_for('settings_page'))
    
    current_user.company_name = request.form.get('company_name', current_user.company_name)
    current_user.email = new_email
    current_user.invoice_prefix = request.form.get('invoice_prefix', current_user.invoice_prefix)
    current_user.email_notifications_enabled = request.form.get('email_notifications_enabled') == 'on'

    new_password = request.form.get('new_password')
    if new_password:
        current_user.set_password(new_password)

    try:
        db.session.commit()
        flash('Settings updated successfully', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating settings: {str(e)}', 'error')
    
    return redirect(url_for('settings_page'))


# ==================== NOTIFICATION ROUTES ====================

@app.route('/notifications')
@user_required
def notifications_page():
    notifications = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(Notification.created_at.desc()).all()
    return render_template('notifications.html', notifications=notifications)


@app.route('/notifications/<int:id>/read', methods=['POST'])
@user_required
def mark_notification_read(id):
    notification = Notification.query.filter_by(
        id=id,
        user_id=current_user.id
    ).first_or_404()
    notification.is_read = True
    db.session.commit()
    return redirect(url_for('notifications_page'))


@app.route('/test-notification')
@user_required
def test_notif():
    """Create a test notification and attempt email, surfacing any errors"""
    # Save in-app notification first (no email)
    create_notification(
        current_user.id,
        "Test Notification",
        "This is a test notification to check if the system is working!",
        send_email=False
    )

    # Attempt email separately so we can show the real error
    mail_user = app.config.get('MAIL_USERNAME')
    mail_pass = app.config.get('MAIL_PASSWORD')

    if not mail_user or not mail_pass:
        flash(f'Notification saved. Email skipped: MAIL_USERNAME={repr(mail_user)}, MAIL_PASSWORD set={bool(mail_pass)}', 'info')
    else:
        try:
            html_body = (
                "<html><body style='font-family:Arial,sans-serif;line-height:1.6;color:#333;background:#f0f2f5;margin:0;padding:0;'>"
                "<div style='max-width:640px;margin:32px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);'>"
                "<div style='background:#4a6fa5;padding:28px 32px;'>"
                "<h1 style='margin:0;color:#fff;font-size:22px;font-weight:700;'>Test Notification</h1>"
                "</div>"
                "<div style='padding:28px 32px;'>"
                "<p style='font-size:15px;color:#444;'>This is a test email from InvoiceFlow to verify your mail configuration is working correctly.</p>"
                "<p style='font-size:14px;color:#666;'>If you received this, emails are set up and sending successfully.</p>"
                "</div>"
                "<div style='padding:20px 32px;background:#f8f9fa;border-top:1px solid #e8ecf0;'>"
                "<p style='margin:0;font-size:12px;color:#666;'><strong>InvoiceFlow</strong><br>"
                "This is an automated notification from your invoice management system.<br>"
                "<a href='http://localhost:5000/notifications' style='color:#4a6fa5;text-decoration:none;'>View all notifications &rarr;</a>"
                "</p></div>"
                "</div></body></html>"
            )
            send_email_direct(current_user.email, "InvoiceFlow Notification: Test Notification", html_body)
            flash(f'Notification created and email sent to {current_user.email}!', 'success')
        except Exception as e:
            flash(f'Notification saved but email failed: {str(e)}', 'error')

    return redirect(url_for('notifications_page'))


# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def page_not_found(e):
    try:
        return render_template('errors/404.html', error=e), 404
    except:
        return 'Page not found', 404


# ==================== INITIALIZATION ====================

with app.app_context():
    try:
        db.create_all()

        # Schedule the recurring check every hour
        try:
            scheduler.add_job(
                id='process_recurring',
                func=process_recurring_invoices,
                trigger='interval',
                hours=1,
                replace_existing=True
            )
            print("✓ Recurring invoice scheduler started (Runs every 1 hour)")
        except Exception as e:
            print(f"Scheduler Warning: {e}")

        if not User.query.filter_by(username='admin').first():
            admin = User(
                username='admin',
                email='admin@example.com',
                company_name='My Company',
                role='admin'
            )
            admin.set_password('admin123')
            db.session.add(admin)
            print("✓ Created default admin user: admin / admin123")

        if not User.query.filter_by(username='customer').first():
            customer = User(
                username='customer',
                email='customer@example.com',
                company_name='My Company',
                role='customer'
            )
            customer.set_password('customer123')
            db.session.add(customer)
            print("✓ Created default customer user: customer / customer123")

        db.session.commit()
        print("✓ Database setup completed successfully!")

    except Exception as e:
        print(f"✗ Error setting up database: {e}")

if __name__ == '__main__':
    app.run(debug=True, port=5000)