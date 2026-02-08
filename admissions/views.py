import base64
import hashlib
import hmac
import io
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import AdmissionForm, StudentLoginForm
from .models import (
    Admission,
    Batch,
    ClassLevel,
    FeePlan,
    Medium,
    Notice,
    Payment,
    PaymentGateway,
    PaymentMethod,
    PaymentStatus,
    Student,
)

def home(request):
    selected_class = request.GET.get("class", ClassLevel.CLASS_12)
    selected_medium = request.GET.get("medium", Medium.HINDI)
    valid_classes = {choice[0] for choice in ClassLevel.choices}
    valid_mediums = {choice[0] for choice in Medium.choices}
    if selected_class not in valid_classes:
        selected_class = ClassLevel.CLASS_12
    if selected_medium not in valid_mediums:
        selected_medium = Medium.HINDI

    try:
        plan = FeePlan.objects.get(
            student_class=selected_class,
            medium=selected_medium,
        )
    except FeePlan.DoesNotExist:
        plan = None
    today = date.today()

    offer_active = bool(plan and today <= plan.offer_end_date)
    show_offer = bool(
        plan
        and offer_active
        and plan.offer_fee is not None
        and plan.original_fee is not None
        and plan.offer_fee < plan.original_fee
    )

    batches = list(
        Batch.objects.filter(
            student_class=selected_class,
            medium=selected_medium,
        ).order_by("name")
    )
    total_remaining = 0
    for batch in batches:
        total_remaining += batch.remaining_seats

    notices = Notice.objects.filter(
        is_active=True,
        start_date__lte=today,
    ).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today)
    ).order_by("-created_at")

    context = {
        "plan_missing": plan is None,
        "class_choices": ClassLevel.choices,
        "medium_choices": Medium.choices,
        "selected_class": selected_class,
        "selected_medium": selected_medium,
        "original_fee": plan.original_fee if plan else None,
        "offer_fee": plan.offer_fee if plan else None,
        "offer_end": plan.offer_end_date if plan else None,
        "offer_active": offer_active,
        "show_offer": show_offer,
        "batches": batches,
        "total_remaining": total_remaining,
        "notices": notices,
    }
    return render(request, "home.html", context)


def admission(request):
    if request.method == "POST":
        form = AdmissionForm(request.POST)
        if form.is_valid():
            fee_amount = 0
            offer_applied = False
            try:
                plan = FeePlan.objects.get(
                    student_class=form.cleaned_data["student_class"],
                    medium=form.cleaned_data["medium"],
                )
                today = date.today()
                offer_applied = (
                    today <= plan.offer_end_date and plan.offer_fee < plan.original_fee
                )
                fee_amount = plan.offer_fee if offer_applied else plan.original_fee
            except FeePlan.DoesNotExist:
                messages.warning(
                    request,
                    "Fee plan missing for selected class and medium. Please contact admin.",
                )
            with transaction.atomic():
                student = Student.objects.create(
                    name=form.cleaned_data["name"],
                    mobile=form.cleaned_data["mobile"],
                    whatsapp=form.cleaned_data["whatsapp"],
                    address=form.cleaned_data["address"],
                )
                admission = Admission.objects.create(
                    student=student,
                    student_class=form.cleaned_data["student_class"],
                    board=form.cleaned_data["board"],
                    medium=form.cleaned_data["medium"],
                    batch=form.cleaned_data["batch"],
                    fee_amount=fee_amount,
                )
                Payment.objects.create(
                    admission=admission,
                    amount=fee_amount,
                    status=PaymentStatus.PENDING,
                )
                if admission.batch:
                    admission.batch.filled_seats += 1
                    admission.batch.save(update_fields=["filled_seats"])
            messages.success(request, "Admission submitted successfully.")
            if offer_applied:
                messages.info(request, "Offer fee applied for this admission.")
            return redirect("admission_success", admission_id=admission.id)
    else:
        form = AdmissionForm()

    return render(
        request,
        "admission_form.html",
        {
            "form": form,
            "no_batches": getattr(form, "no_batches", False),
        },
    )


def admission_success(request, admission_id):
    admission = Admission.objects.select_related("student", "batch").get(id=admission_id)
    payment = Payment.objects.filter(admission=admission).first()

    upi_id = getattr(settings, "PAYMENT_UPI_ID", "")
    receiver_name = getattr(settings, "PAYMENT_RECEIVER_NAME", "Pradhan Chemistry Classes")
    upi_link = None
    if upi_id and admission.fee_amount:
        params = {
            "pa": upi_id,
            "pn": receiver_name,
            "am": str(admission.fee_amount),
            "cu": "INR",
            "tn": f"Admission {admission.id}",
        }
        upi_link = "upi://pay?" + "&".join(
            f"{key}={quote(value)}" for key, value in params.items()
        )

    if payment and payment.status == PaymentStatus.PAID and not payment.paid_at:
        payment.paid_at = timezone.now()
        payment.save(update_fields=["paid_at"])

    gateway = getattr(settings, "PAYMENT_GATEWAY", PaymentGateway.RAZORPAY)
    if gateway == PaymentGateway.PHONEPE:
        online_payment_ready = all(
            [
                getattr(settings, "PHONEPE_MERCHANT_ID", ""),
                getattr(settings, "PHONEPE_SALT_KEY", ""),
                getattr(settings, "PHONEPE_SALT_INDEX", ""),
                getattr(settings, "PHONEPE_BASE_URL", ""),
            ]
        )
    else:
        online_payment_ready = bool(
            getattr(settings, "RAZORPAY_KEY_ID", "")
            and getattr(settings, "RAZORPAY_KEY_SECRET", "")
        )

    return render(
        request,
        "admission_success.html",
        {
            "admission": admission,
            "payment": payment,
            "upi_link": upi_link,
            "upi_id": upi_id,
            "online_payment_ready": online_payment_ready,
            "payment_gateway": gateway,
        },
    )


def start_payment(request, admission_id):
    admission = Admission.objects.select_related("student").get(id=admission_id)
    payment = Payment.objects.filter(admission=admission).first()
    if not payment:
        payment = Payment.objects.create(
            admission=admission,
            amount=admission.fee_amount,
            status=PaymentStatus.PENDING,
        )

    if payment.status == PaymentStatus.PAID:
        messages.info(request, "Payment already completed.")
        return redirect("admission_success", admission_id=admission_id)

    if admission.fee_amount <= 0:
        messages.warning(request, "Fee amount is not set for this admission.")
        return redirect("admission_success", admission_id=admission_id)

    gateway = getattr(settings, "PAYMENT_GATEWAY", PaymentGateway.RAZORPAY)
    if gateway == PaymentGateway.PHONEPE:
        try:
            redirect_url = _start_phonepe_payment(request, admission, payment)
        except RuntimeError as exc:
            messages.error(request, str(exc))
            return redirect("admission_success", admission_id=admission_id)
        return redirect(redirect_url)

    key_id, key_secret = _get_razorpay_credentials()
    if not key_id or not key_secret:
        messages.warning(request, "Razorpay keys are missing in settings.")
        return redirect("admission_success", admission_id=admission_id)

    amount_paise = int(admission.fee_amount) * 100
    try:
        order = _create_razorpay_order(
            amount_paise,
            receipt=f"admission_{admission.id}",
            key_id=key_id,
            key_secret=key_secret,
        )
    except RuntimeError as exc:
        messages.error(request, str(exc))
        return redirect("admission_success", admission_id=admission_id)

    payment.gateway = PaymentGateway.RAZORPAY
    payment.order_id = order.get("id", "")
    payment.amount = admission.fee_amount
    payment.method = PaymentMethod.ONLINE
    payment.status = PaymentStatus.PENDING
    payment.save(update_fields=["gateway", "order_id", "amount", "method", "status"])

    context = {
        "admission": admission,
        "payment": payment,
        "razorpay_key_id": key_id,
        "razorpay_amount": amount_paise,
        "razorpay_order_id": payment.order_id,
        "razorpay_name": "Pradhan Chemistry Classes",
    }
    return render(request, "payment_checkout.html", context)


@require_POST
def payment_verify(request):
    order_id = request.POST.get("razorpay_order_id", "")
    payment_id = request.POST.get("razorpay_payment_id", "")
    signature = request.POST.get("razorpay_signature", "")

    payment = Payment.objects.filter(order_id=order_id).select_related("admission").first()
    if not payment:
        messages.error(request, "Payment record not found.")
        return redirect("home")

    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")
    if not _verify_razorpay_signature(order_id, payment_id, signature, key_secret):
        payment.status = PaymentStatus.FAILED
        payment.payment_id = payment_id
        payment.signature = signature
        payment.save(update_fields=["status", "payment_id", "signature"])
        messages.error(request, "Payment verification failed.")
        return redirect("admission_success", admission_id=payment.admission.id)

    payment.status = PaymentStatus.PAID
    payment.payment_id = payment_id
    payment.signature = signature
    payment.paid_at = timezone.now()
    payment.save(update_fields=["status", "payment_id", "signature", "paid_at"])

    admission = payment.admission
    admission.fee_status = "Paid"
    admission.fee_paid = payment.amount
    admission.save(update_fields=["fee_status", "fee_paid"])

    _maybe_send_payment_notifications(admission, payment)

    messages.success(request, "Payment successful.")
    return redirect("admission_success", admission_id=admission.id)


def student_login(request):
    if request.method == "POST":
        form = StudentLoginForm(request.POST)
        if form.is_valid():
            admission_id = form.cleaned_data["admission_id"]
            mobile = form.cleaned_data["mobile"]
            admission = Admission.objects.select_related("student", "batch").filter(
                id=admission_id,
                student__mobile=mobile,
            ).first()
            if admission:
                request.session["student_admission_id"] = admission.id
                return redirect("student_dashboard")
            messages.error(request, "No admission found with this ID and mobile.")
    else:
        form = StudentLoginForm()

    return render(request, "student_login.html", {"form": form})


def student_dashboard(request):
    admission_id = request.session.get("student_admission_id")
    if not admission_id:
        return redirect("student_login")

    admission = Admission.objects.select_related("student", "batch").get(id=admission_id)
    payment = Payment.objects.filter(admission=admission).first()

    today = date.today()
    notices = Notice.objects.filter(
        is_active=True,
        start_date__lte=today,
    ).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today)
    ).order_by("-created_at")

    gateway = getattr(settings, "PAYMENT_GATEWAY", PaymentGateway.RAZORPAY)
    if gateway == PaymentGateway.PHONEPE:
        online_payment_ready = all(
            [
                getattr(settings, "PHONEPE_MERCHANT_ID", ""),
                getattr(settings, "PHONEPE_SALT_KEY", ""),
                getattr(settings, "PHONEPE_SALT_INDEX", ""),
                getattr(settings, "PHONEPE_BASE_URL", ""),
            ]
        )
    else:
        online_payment_ready = bool(
            getattr(settings, "RAZORPAY_KEY_ID", "")
            and getattr(settings, "RAZORPAY_KEY_SECRET", "")
        )

    return render(
        request,
        "student_dashboard.html",
        {
            "admission": admission,
            "payment": payment,
            "notices": notices,
            "online_payment_ready": online_payment_ready,
            "payment_gateway": gateway,
        },
    )


def student_logout(request):
    request.session.pop("student_admission_id", None)
    return redirect("student_login")


def receipt_pdf(request, admission_id):
    admission = Admission.objects.select_related("student", "batch").get(id=admission_id)
    payment = Payment.objects.filter(admission=admission).first()
    try:
        from reportlab.graphics import renderPDF
        from reportlab.graphics.barcode import qr
        from reportlab.graphics.shapes import Drawing
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
    except Exception:
        return HttpResponse(
            "PDF generation not available. Please install reportlab.",
            status=501,
        )

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    title = getattr(settings, "INVOICE_TITLE", "Pradhan Chemistry Classes")
    subtitle = getattr(settings, "INVOICE_SUBTITLE", "Fee Receipt")
    address = getattr(settings, "INVOICE_ADDRESS", "")
    logo_path = _invoice_asset(getattr(settings, "INVOICE_LOGO_PATH", ""))
    signature_path = _invoice_asset(getattr(settings, "INVOICE_SIGNATURE_PATH", ""))
    stamp_path = _invoice_asset(getattr(settings, "INVOICE_STAMP_PATH", ""))

    primary = colors.HexColor("#103946")
    accent = colors.HexColor("#f2a33a")
    dark = colors.HexColor("#1b1a17")
    muted = colors.HexColor("#5d594f")
    line_color = colors.HexColor("#e2d7cc")
    glass = colors.HexColor("#fbf8f3")
    white = colors.white

    margin = 36
    content_w = width - (2 * margin)
    col_gap = 20
    col_w = (content_w - col_gap) / 2

    def wrap_text(text, max_width, font_name, font_size):
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            if pdf.stringWidth(test, font_name, font_size) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def draw_badge(x, y, text, color):
        pdf.setFillColor(color)
        pdf.roundRect(x, y, 72, 18, 8, stroke=0, fill=1)
        pdf.setFillColor(white)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawCentredString(x + 36, y + 5, text)

    def draw_section_title(x, y, text):
        pdf.setFillColor(primary)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(x, y, text)
        pdf.setStrokeColor(line_color)
        pdf.setLineWidth(1)
        pdf.line(x, y - 6, x + col_w, y - 6)
        return y - 20

    def draw_kv_list(x, y, items):
        for label, value in items:
            pdf.setFont("Helvetica", 8)
            pdf.setFillColor(muted)
            pdf.drawString(x, y, label.upper())
            pdf.setFont("Helvetica-Bold", 11)
            pdf.setFillColor(dark)
            pdf.drawString(x, y - 12, value)
            y -= 26
        return y

    watermark_text = getattr(settings, "INVOICE_WATERMARK_TEXT", "").strip() or title
    if watermark_text:
        pdf.saveState()
        pdf.setFont("Helvetica-Bold", 60)
        pdf.setFillColor(colors.HexColor("#d8c9ba"))
        if hasattr(pdf, "setFillAlpha"):
            pdf.setFillAlpha(0.18)
        pdf.translate(width / 2, height / 2)
        pdf.rotate(26)
        for offset in (-140, 0, 140):
            pdf.drawCentredString(0, offset, watermark_text)
        pdf.restoreState()

    stripe1_h = 18
    stripe2_h = 6
    pdf.setFillColor(primary)
    pdf.rect(0, height - stripe1_h, width, stripe1_h, stroke=0, fill=1)
    pdf.setFillColor(accent)
    pdf.rect(0, height - stripe1_h - stripe2_h, width, stripe2_h, stroke=0, fill=1)

    header_h = 120
    header_top = height - stripe1_h - stripe2_h - 12
    header_y = header_top - header_h
    pdf.setFillColor(white)
    pdf.roundRect(margin, header_y, content_w, header_h, 16, stroke=0, fill=1)
    pdf.setStrokeColor(line_color)
    pdf.roundRect(margin, header_y, content_w, header_h, 16, stroke=1, fill=0)

    logo_x = margin + 16
    logo_y = header_y + 28
    if logo_path:
        pdf.drawImage(
            logo_path,
            logo_x,
            logo_y,
            width=58,
            height=58,
            preserveAspectRatio=True,
            mask="auto",
        )
    else:
        pdf.setFillColor(primary)
        pdf.circle(logo_x + 29, logo_y + 29, 29, stroke=0, fill=1)
        pdf.setFillColor(white)
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawCentredString(logo_x + 29, logo_y + 21, "PC")

    title_x = logo_x + 74
    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(title_x, header_y + header_h - 32, title)
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(muted)
    pdf.drawString(title_x, header_y + header_h - 48, subtitle)
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(muted)

    meta_w = 190
    meta_h = 70
    meta_x = margin + content_w - meta_w - 12
    meta_y = header_y + (header_h - meta_h) / 2
    pdf.setFillColor(glass)
    pdf.setStrokeColor(line_color)
    pdf.roundRect(meta_x, meta_y, meta_w, meta_h, 12, stroke=1, fill=1)

    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 8)
    pdf.drawString(meta_x + 12, meta_y + meta_h - 16, "RECEIPT NO")
    pdf.setFont("Helvetica-Bold", 11)
    pdf.setFillColor(dark)
    pdf.drawString(meta_x + 12, meta_y + meta_h - 30, f"ADM-{admission.id}")
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 8)
    pdf.drawString(meta_x + 12, meta_y + 16, "DATE")
    pdf.setFont("Helvetica-Bold", 10)
    pdf.setFillColor(dark)
    date_text = admission.created_at.strftime("%d %b %Y") if admission.created_at else ""
    pdf.drawString(meta_x + 12, meta_y + 3, date_text)

    status_value = payment.status if payment else "Pending"
    status_color = accent
    if status_value == "Paid":
        status_color = colors.HexColor("#15803d")
    elif status_value == "Failed":
        status_color = colors.HexColor("#b91c1c")
    draw_badge(meta_x + meta_w - 76, meta_y + 8, status_value.upper(), status_color)

    if address:
        address_lines = []
        for chunk in address.split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            max_width = meta_x - title_x - 12
            address_lines.extend(wrap_text(chunk, max_width, "Helvetica", 9))
        line_y = header_y + 32
        for addr_line in address_lines[:2]:
            pdf.drawString(title_x, line_y, addr_line)
            line_y -= 12

    card_h = 320
    card_y = header_y - 26 - card_h
    left_x = margin
    right_x = margin + col_w + col_gap

    pdf.setFillColor(glass)
    pdf.setStrokeColor(line_color)
    pdf.roundRect(left_x, card_y, col_w, card_h, 16, stroke=1, fill=1)
    pdf.roundRect(right_x, card_y, col_w, card_h, 16, stroke=1, fill=1)

    left_y = draw_section_title(left_x + 14, card_y + card_h - 24, "Student Details")
    right_y = draw_section_title(right_x + 14, card_y + card_h - 24, "Fee Summary")

    student_name = admission.student.name.strip().title() if admission.student.name else ""
    batch_text = (
        f"{admission.batch.name} ({admission.batch.timing})"
        if admission.batch
        else "Will be assigned by admin"
    )
    student_items = [
        ("Admission ID", f"ADM-{admission.id}"),
        ("Name", student_name),
        ("Mobile", admission.student.mobile),
        ("WhatsApp", admission.student.whatsapp),
        ("Class", admission.student_class),
        ("Board", admission.board),
        ("Medium", admission.medium),
        ("Batch", batch_text),
    ]
    left_y = draw_kv_list(left_x + 14, left_y, student_items)

    if admission.student.address:
        pdf.setFont("Helvetica", 8)
        pdf.setFillColor(muted)
        pdf.drawString(left_x + 14, left_y, "ADDRESS")
        pdf.setFont("Helvetica-Bold", 10)
        pdf.setFillColor(dark)
        address_lines = wrap_text(admission.student.address, col_w - 28, "Helvetica", 10)
        addr_y = left_y - 12
        for addr_line in address_lines[:2]:
            pdf.drawString(left_x + 14, addr_y, addr_line)
            addr_y -= 14
        left_y = addr_y

    due_amount = max(admission.fee_amount - admission.fee_paid, 0)
    fee_items = [
        ("Fee Amount", f"INR {admission.fee_amount}"),
        ("Fee Paid", f"INR {admission.fee_paid}"),
        ("Amount Due", f"INR {due_amount}"),
        ("Fee Status", admission.fee_status),
    ]
    if payment:
        fee_items.append(("Payment Status", payment.status))
        if payment.reference_id:
            fee_items.append(("Reference ID", payment.reference_id))
        if payment.method:
            fee_items.append(("Method", payment.method))
    right_y = draw_kv_list(right_x + 14, right_y, fee_items)

    qr_template = getattr(settings, "INVOICE_QR_TEMPLATE", "")
    if qr_template:
        qr_text = qr_template.format(
            admission_id=admission.id,
            name=admission.student.name,
            mobile=admission.student.mobile,
            amount=admission.fee_amount,
            status=admission.fee_status,
        )
    else:
        qr_text = (
            f"AdmissionID:{admission.id}|Name:{admission.student.name}|"
            f"Amount:{admission.fee_amount}|Status:{admission.fee_status}"
        )

    if qr_text:
        qr_widget = qr.QrCodeWidget(qr_text)
        qr_size = 92
        bounds = qr_widget.getBounds()
        width_scale = qr_size / (bounds[2] - bounds[0])
        height_scale = qr_size / (bounds[3] - bounds[1])
        drawing = Drawing(qr_size, qr_size, transform=[width_scale, 0, 0, height_scale, 0, 0])
        drawing.add(qr_widget)
        qr_x = right_x + col_w - qr_size - 16
        qr_y = card_y + 16
        renderPDF.draw(drawing, pdf, qr_x, qr_y)
        pdf.setFont("Helvetica", 8)
        pdf.setFillColor(muted)
        pdf.drawRightString(qr_x + qr_size, qr_y - 10, "Scan for details")

    footer_y = margin + 30
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(muted)
    pdf.drawString(
        margin,
        footer_y,
        "This is a system-generated receipt. For any query, contact the institute office.",
    )

    if stamp_path:
        pdf.drawImage(
            stamp_path,
            margin,
            footer_y - 42,
            width=4.5 * cm,
            height=1.8 * cm,
            preserveAspectRatio=True,
            mask="auto",
        )

    if signature_path:
        pdf.setFillColor(dark)
        pdf.setFont("Helvetica", 9)
        pdf.drawString(width - margin - 150, footer_y - 8, "Authorized Signature")
        pdf.drawImage(
            signature_path,
            width - margin - 150,
            footer_y - 42,
            width=4.5 * cm,
            height=1.8 * cm,
            preserveAspectRatio=True,
            mask="auto",
        )

    pdf.showPage()
    pdf.save()
    buffer.seek(0)

    filename = f"invoice-{admission.id}.pdf"
    return FileResponse(buffer, as_attachment=True, filename=filename)


def _get_razorpay_credentials():
    key_id = getattr(settings, "RAZORPAY_KEY_ID", "")
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")
    return key_id, key_secret


def _create_razorpay_order(amount_paise, receipt, key_id, key_secret):
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1,
    }
    data = json.dumps(payload).encode("utf-8")
    auth = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("utf-8")

    request_obj = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise RuntimeError(f"Razorpay order failed: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Razorpay connection failed: {exc}")

    return json.loads(response_body)


def _verify_razorpay_signature(order_id, payment_id, signature, key_secret):
    if not (order_id and payment_id and signature and key_secret):
        return False
    message = f"{order_id}|{payment_id}".encode("utf-8")
    generated = hmac.new(
        key_secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(generated, signature)


def _start_phonepe_payment(request, admission, payment):
    merchant_id, salt_key, salt_index, base_url = _get_phonepe_config()
    if not merchant_id or not salt_key or not salt_index or not base_url:
        raise RuntimeError("PhonePe settings are missing in settings.py.")

    if not payment.order_id:
        payment.order_id = f"ADM{admission.id}{int(time.time())}"

    amount_paise = int(admission.fee_amount) * 100
    redirect_url = request.build_absolute_uri(
        reverse("admission_success", kwargs={"admission_id": admission.id})
    )
    callback_url = request.build_absolute_uri(reverse("phonepe_callback"))

    payload = {
        "merchantId": merchant_id,
        "merchantTransactionId": payment.order_id,
        "merchantUserId": str(admission.student.id),
        "amount": amount_paise,
        "redirectUrl": redirect_url,
        "redirectMode": "POST",
        "callbackUrl": callback_url,
        "mobileNumber": admission.student.mobile,
        "paymentInstrument": {
            "type": "PAY_PAGE",
        },
    }

    response = _phonepe_post_request(
        base_url,
        "/pg/v1/pay",
        payload,
        salt_key,
        salt_index,
    )

    payment.gateway = PaymentGateway.PHONEPE
    payment.amount = admission.fee_amount
    payment.method = PaymentMethod.ONLINE
    payment.status = PaymentStatus.PENDING
    payment.gateway_response = json.dumps(response)
    payment.save(update_fields=["gateway", "amount", "method", "status", "order_id", "gateway_response"])

    redirect_info = (
        response.get("data", {})
        .get("instrumentResponse", {})
        .get("redirectInfo", {})
    )
    redirect_url = redirect_info.get("url")
    if not redirect_url:
        raise RuntimeError("PhonePe pay URL not received.")

    return redirect_url


@csrf_exempt
@require_POST
def phonepe_callback(request):
    raw_body = request.body.decode("utf-8")
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return HttpResponse("Invalid payload", status=400)

    response_b64 = data.get("response")
    if not response_b64:
        return HttpResponse("Missing response", status=400)

    merchant_id, salt_key, salt_index, _ = _get_phonepe_config()
    if not merchant_id or not salt_key or not salt_index:
        return HttpResponse("PhonePe not configured", status=400)

    if getattr(settings, "PHONEPE_VERIFY_CALLBACK", True):
        header = request.headers.get("X-VERIFY") or request.headers.get("x-verify")
        if not _verify_phonepe_callback(response_b64, header, salt_key, salt_index):
            return HttpResponse("Signature mismatch", status=400)

    try:
        decoded = json.loads(base64.b64decode(response_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse("Invalid response data", status=400)

    transaction_data = decoded.get("data", {})
    merchant_txn_id = transaction_data.get("merchantTransactionId")
    if not merchant_txn_id:
        return HttpResponse("Missing transaction", status=400)

    payment = Payment.objects.filter(order_id=merchant_txn_id).select_related("admission").first()
    if not payment:
        return HttpResponse("Payment not found", status=404)

    _update_payment_from_phonepe(payment, decoded)
    return HttpResponse("OK")


def phonepe_status_check(request, admission_id):
    payment = Payment.objects.filter(admission_id=admission_id).select_related("admission").first()
    if not payment or not payment.order_id:
        messages.warning(request, "PhonePe payment not found for this admission.")
        return redirect("admission_success", admission_id=admission_id)

    try:
        response = _phonepe_fetch_status(payment.order_id)
    except RuntimeError as exc:
        messages.error(request, str(exc))
        return redirect("admission_success", admission_id=admission_id)

    _update_payment_from_phonepe(payment, response)
    messages.info(request, "PhonePe status updated.")
    return redirect("admission_success", admission_id=admission_id)


def _update_payment_from_phonepe(payment, decoded):
    transaction_data = decoded.get("data", {})
    state = transaction_data.get("state") or decoded.get("state", "")
    response_code = transaction_data.get("responseCode") or decoded.get("code", "")
    payment_id = transaction_data.get("transactionId", "")
    reference_id = transaction_data.get("utr", "") or transaction_data.get("providerReferenceId", "")

    payment.gateway = PaymentGateway.PHONEPE
    payment.payment_id = payment_id
    payment.reference_id = reference_id
    payment.gateway_response = json.dumps(decoded)

    status = _map_phonepe_status(state, response_code)
    payment.status = status
    if status == PaymentStatus.PAID:
        payment.paid_at = timezone.now()
    payment.save(update_fields=["gateway", "payment_id", "reference_id", "gateway_response", "status", "paid_at"])

    if status == PaymentStatus.PAID:
        admission = payment.admission
        admission.fee_status = "Paid"
        admission.fee_paid = payment.amount
        admission.save(update_fields=["fee_status", "fee_paid"])
        _maybe_send_payment_notifications(admission, payment)


def _map_phonepe_status(state, response_code):
    state_upper = str(state).upper()
    code_upper = str(response_code).upper()
    if state_upper in {"COMPLETED", "SUCCESS"} or code_upper in {"PAYMENT_SUCCESS", "SUCCESS"}:
        return PaymentStatus.PAID
    if state_upper in {"FAILED", "ERROR"} or code_upper in {"PAYMENT_ERROR", "FAILED"}:
        return PaymentStatus.FAILED
    return PaymentStatus.PENDING


def _get_phonepe_config():
    merchant_id = getattr(settings, "PHONEPE_MERCHANT_ID", "")
    salt_key = getattr(settings, "PHONEPE_SALT_KEY", "")
    salt_index = getattr(settings, "PHONEPE_SALT_INDEX", "")
    base_url = getattr(settings, "PHONEPE_BASE_URL", "")
    return merchant_id, salt_key, salt_index, base_url


def _phonepe_post_request(base_url, api_path, payload, salt_key, salt_index):
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.b64encode(payload_json).decode("utf-8")
    checksum = _phonepe_checksum(payload_b64, api_path, salt_key, salt_index)

    body = json.dumps({"request": payload_b64}).encode("utf-8")
    request_obj = urllib.request.Request(
        f"{base_url}{api_path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-VERIFY": checksum,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise RuntimeError(f"PhonePe request failed: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PhonePe connection failed: {exc}")

    return json.loads(response_body)


def _phonepe_fetch_status(merchant_transaction_id):
    merchant_id, salt_key, salt_index, base_url = _get_phonepe_config()
    if not merchant_id or not salt_key or not salt_index or not base_url:
        raise RuntimeError("PhonePe settings are missing in settings.py.")

    api_path = f"/pg/v1/status/{merchant_id}/{merchant_transaction_id}"
    checksum = _phonepe_checksum("", api_path, salt_key, salt_index)

    request_obj = urllib.request.Request(
        f"{base_url}{api_path}",
        headers={
            "Content-Type": "application/json",
            "X-VERIFY": checksum,
            "X-MERCHANT-ID": merchant_id,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise RuntimeError(f"PhonePe status failed: {detail}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PhonePe connection failed: {exc}")

    return json.loads(response_body)


def _phonepe_checksum(payload_b64, api_path, salt_key, salt_index):
    raw = f"{payload_b64}{api_path}{salt_key}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return f"{digest}###{salt_index}"


def _verify_phonepe_callback(response_b64, header_value, salt_key, salt_index):
    if not header_value:
        return False
    raw = f"{response_b64}{salt_key}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    expected = f"{digest}###{salt_index}"
    return hmac.compare_digest(expected, header_value)


def _maybe_send_payment_notifications(admission, payment):
    if not getattr(settings, "SEND_PAYMENT_NOTIFICATIONS", True):
        return
    if payment.notified_at:
        return

    sms_sent = False
    whatsapp_sent = False
    message = (
        f"Payment received for Admission #{admission.id}. "
        f"Amount: INR {payment.amount}. "
        "Thank you - Pradhan Chemistry Classes."
    )

    if _sms_configured():
        sms_sent = _send_sms(admission.student.mobile, message)
    if _whatsapp_configured():
        whatsapp_sent = _send_whatsapp(admission.student.mobile, message)

    if sms_sent or whatsapp_sent:
        payment.notified_at = timezone.now()
        payment.save(update_fields=["notified_at"])


def _sms_configured():
    provider = getattr(settings, "SMS_PROVIDER", "")
    if provider.lower() == "twilio":
        return bool(
            getattr(settings, "TWILIO_ACCOUNT_SID", "")
            and getattr(settings, "TWILIO_AUTH_TOKEN", "")
            and getattr(settings, "TWILIO_FROM_NUMBER", "")
        )
    return False


def _whatsapp_configured():
    provider = getattr(settings, "WHATSAPP_PROVIDER", "")
    if provider.lower() == "cloud":
        return bool(
            getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")
            and getattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
        )
    return False


def _send_sms(to_number, message):
    provider = getattr(settings, "SMS_PROVIDER", "").lower()
    if provider == "twilio":
        return _send_sms_twilio(to_number, message)
    return False


def _send_whatsapp(to_number, message):
    provider = getattr(settings, "WHATSAPP_PROVIDER", "").lower()
    if provider == "cloud":
        return _send_whatsapp_cloud(to_number, message)
    return False


def _format_e164(number):
    number = str(number).strip()
    if number.startswith("+"):
        return number
    default_cc = getattr(settings, "DEFAULT_COUNTRY_CODE", "+91")
    return f"{default_cc}{number}"


def _send_sms_twilio(to_number, message):
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    from_number = getattr(settings, "TWILIO_FROM_NUMBER", "")
    if not account_sid or not auth_token or not from_number:
        return False

    payload = urlencode(
        {
            "To": _format_e164(to_number),
            "From": from_number,
            "Body": message,
        }
    ).encode("utf-8")
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("utf-8")

    request_obj = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=payload,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            return 200 <= response.status < 300
    except urllib.error.URLError:
        return False


def _send_whatsapp_cloud(to_number, message):
    phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")
    access_token = getattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
    api_version = getattr(settings, "WHATSAPP_API_VERSION", "v19.0")
    if not phone_number_id or not access_token:
        return False

    payload = json.dumps(
        {
            "messaging_product": "whatsapp",
            "to": _format_e164(to_number).replace("+", ""),
            "type": "text",
            "text": {"body": message},
        }
    ).encode("utf-8")

    request_obj = urllib.request.Request(
        f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages",
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            return 200 <= response.status < 300
    except urllib.error.URLError:
        return False


def _invoice_asset(path_value):
    if not path_value:
        return ""
    if os.path.isabs(path_value):
        return path_value if os.path.exists(path_value) else ""
    full_path = os.path.join(settings.BASE_DIR, path_value)
    return full_path if os.path.exists(full_path) else ""
