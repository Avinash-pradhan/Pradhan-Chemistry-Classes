from django.contrib import admin
from django.utils import timezone

from .models import Admission, Batch, FeePlan, Notice, Payment, PaymentStatus, Student


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ("name", "mobile", "whatsapp", "created_at")
    search_fields = ("name", "mobile", "whatsapp")
    ordering = ("-created_at",)


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("name", "student_class", "medium", "timing", "filled_seats", "total_seats")
    list_filter = ("student_class", "medium")
    ordering = ("name",)


@admin.register(FeePlan)
class FeePlanAdmin(admin.ModelAdmin):
    list_display = ("student_class", "medium", "original_fee", "offer_fee", "offer_end_date")
    list_filter = ("student_class", "medium")
    ordering = ("student_class", "medium")


@admin.register(Admission)
class AdmissionAdmin(admin.ModelAdmin):
    list_display = (
        "student_name",
        "student_class",
        "board",
        "medium",
        "batch",
        "fee_amount",
        "fee_status",
        "created_at",
    )
    list_filter = ("student_class", "board", "medium", "fee_status", "batch")
    search_fields = ("student__name", "student__mobile", "student__whatsapp")
    ordering = ("-created_at",)
    list_select_related = ("student", "batch")

    @admin.display(description="Student")
    def student_name(self, obj):
        return obj.student.name


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "admission",
        "amount",
        "gateway",
        "status",
        "method",
        "reference_id",
        "order_id",
        "payment_id",
        "notified_at",
        "created_at",
        "paid_at",
    )
    list_filter = ("status", "method", "gateway")
    search_fields = ("admission__student__name", "reference_id")
    ordering = ("-created_at",)
    actions = ("mark_paid",)

    @admin.action(description="Mark selected payments as Paid")
    def mark_paid(self, request, queryset):
        for payment in queryset:
            payment.status = PaymentStatus.PAID
            if not payment.paid_at:
                payment.paid_at = timezone.now()
            payment.save(update_fields=["status", "paid_at"])
            admission = payment.admission
            admission.fee_status = "Paid"
            admission.fee_paid = payment.amount
            admission.save(update_fields=["fee_status", "fee_paid"])


@admin.register(Notice)
class NoticeAdmin(admin.ModelAdmin):
    list_display = ("title", "start_date", "end_date", "is_active", "created_at")
    list_filter = ("is_active", "start_date", "end_date")
    search_fields = ("title", "message")
    ordering = ("-created_at",)


