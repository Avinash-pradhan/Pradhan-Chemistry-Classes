from django import forms
from django.core.validators import RegexValidator

from .models import Batch, Board, ClassLevel, Medium


phone_validator = RegexValidator(
    regex=r"^\d{10}$",
    message="Enter a 10-digit mobile number.",
)


class AdmissionForm(forms.Form):
    name = forms.CharField(max_length=100)
    student_class = forms.ChoiceField(choices=ClassLevel.choices, label="Class")
    board = forms.ChoiceField(choices=Board.choices)
    medium = forms.ChoiceField(choices=Medium.choices)
    mobile = forms.CharField(max_length=10, validators=[phone_validator])
    whatsapp = forms.CharField(max_length=10, validators=[phone_validator])
    address = forms.CharField(widget=forms.Textarea, required=False)
    batch = forms.ModelChoiceField(queryset=Batch.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        batches = Batch.objects.order_by("name")
        self.no_batches = not batches.exists()
        if self.no_batches:
            self.fields["batch"].required = False
            self.fields["batch"].widget = forms.HiddenInput()
        else:
            self.fields["batch"].queryset = batches

    def clean_batch(self):
        batch = self.cleaned_data.get("batch")
        if batch and batch.is_full:
            raise forms.ValidationError("This batch is full. Please choose another.")
        return batch

    def clean(self):
        cleaned_data = super().clean()
        batch = cleaned_data.get("batch")
        medium = cleaned_data.get("medium")
        student_class = cleaned_data.get("student_class")
        if batch:
            if batch.medium != medium or batch.student_class != student_class:
                self.add_error(
                    "batch",
                    "Selected batch does not match the chosen class and medium.",
                )
        return cleaned_data


class StudentLoginForm(forms.Form):
    admission_id = forms.IntegerField(label="Admission ID")
    mobile = forms.CharField(max_length=10, validators=[phone_validator])
