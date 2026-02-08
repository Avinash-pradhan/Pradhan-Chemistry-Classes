from datetime import date

from django.db import models


class ClassLevel(models.TextChoices):
    CLASS_11 = "11", "11"
    CLASS_12 = "12", "12"


class Board(models.TextChoices):
    CBSE = "CBSE", "CBSE"
    BSEB = "BSEB", "BSEB"


class Medium(models.TextChoices):
    HINDI = "Hindi", "Hindi"
    ENGLISH = "English", "English"


class FeeStatus(models.TextChoices):
    PENDING = "Pending", "Pending"
    PAID = "Paid", "Paid"


class PaymentStatus(models.TextChoices):
    PENDING = "Pending", "Pending"
    PAID = "Paid", "Paid"
    FAILED = "Failed", "Failed"


class PaymentMethod(models.TextChoices):
    UPI = "UPI", "UPI"
    CASH = "Cash", "Cash"
    BANK = "Bank", "Bank Transfer"
    ONLINE = "Online", "Online"


class PaymentGateway(models.TextChoices):
    RAZORPAY = "Razorpay", "Razorpay"
    PHONEPE = "PhonePe", "PhonePe"


class Student(models.Model):
    name = models.CharField(max_length=100)
    mobile = models.CharField(max_length=10)
    whatsapp = models.CharField(max_length=10)
    address = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.mobile})"


class Batch(models.Model):
    name = models.CharField(max_length=50)
    medium = models.CharField(max_length=20, choices=Medium.choices)
    student_class = models.CharField(
        max_length=2,
        choices=ClassLevel.choices,
        default=ClassLevel.CLASS_12,
    )
    timing = models.CharField(max_length=50)
    total_seats = models.PositiveIntegerField()
    filled_seats = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.name} - {self.student_class} {self.medium}"

    @property
    def is_full(self):
        return self.filled_seats >= self.total_seats

    @property
    def remaining_seats(self):
        return max(self.total_seats - self.filled_seats, 0)


class FeePlan(models.Model):
    student_class = models.CharField(
        max_length=2,
        choices=ClassLevel.choices,
        default=ClassLevel.CLASS_12,
    )
    medium = models.CharField(max_length=20, choices=Medium.choices)
    original_fee = models.PositiveIntegerField()
    offer_fee = models.PositiveIntegerField()
    offer_end_date = models.DateField()

    def __str__(self):
        return f"{self.student_class} {self.medium}"


def get_fee(student_class, medium):
    plan = FeePlan.objects.get(student_class=student_class, medium=medium)
    today = date.today()

    if today <= plan.offer_end_date:
        return plan.offer_fee, True
    return plan.original_fee, False


class Admission(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    student_class = models.CharField(
        max_length=2,
        choices=ClassLevel.choices,
        default=ClassLevel.CLASS_12,
    )
    board = models.CharField(
        max_length=10,
        choices=Board.choices,
        default=Board.CBSE,
    )
    medium = models.CharField(
        max_length=20,
        choices=Medium.choices,
        default=Medium.HINDI,
    )
    batch = models.ForeignKey(Batch, on_delete=models.PROTECT, null=True, blank=True)
    fee_amount = models.PositiveIntegerField(default=0)
    fee_paid = models.PositiveIntegerField(default=0)
    fee_status = models.CharField(
        max_length=10,
        choices=FeeStatus.choices,
        default=FeeStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f"{self.student.name} - {self.student_class} {self.medium}"


class Notice(models.Model):
    title = models.CharField(max_length=120)
    message = models.TextField()
    start_date = models.DateField(default=date.today)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class Payment(models.Model):
    admission = models.OneToOneField(Admission, on_delete=models.CASCADE)
    amount = models.PositiveIntegerField(default=0)
    gateway = models.CharField(
        max_length=20,
        choices=PaymentGateway.choices,
        blank=True,
    )
    status = models.CharField(
        max_length=10,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
    )
    method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        blank=True,
    )
    reference_id = models.CharField(max_length=100, blank=True)
    order_id = models.CharField(max_length=100, blank=True)
    payment_id = models.CharField(max_length=100, blank=True)
    signature = models.CharField(max_length=200, blank=True)
    gateway_response = models.TextField(blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Payment {self.admission_id} - {self.status}"
