from django.urls import path

from .views import (
    admission,
    admission_success,
    home,
    phonepe_callback,
    phonepe_status_check,
    payment_verify,
    receipt_pdf,
    start_payment,
    student_dashboard,
    student_login,
    student_logout,
)

urlpatterns = [
    path('', home, name='home'),
    path('admission/', admission, name='admission'),
    path('admissions/', admission, name='admission_alt'),
    path('admission/success/<int:admission_id>/', admission_success, name='admission_success'),
    path('payment/start/<int:admission_id>/', start_payment, name='start_payment'),
    path('payment/verify/', payment_verify, name='payment_verify'),
    path('payment/phonepe/callback/', phonepe_callback, name='phonepe_callback'),
    path('payment/phonepe/status/<int:admission_id>/', phonepe_status_check, name='phonepe_status_check'),
    path('receipt/<int:admission_id>/pdf/', receipt_pdf, name='receipt_pdf'),
    path('student/', student_login, name='student_login'),
    path('student/dashboard/', student_dashboard, name='student_dashboard'),
    path('student/logout/', student_logout, name='student_logout'),
]
