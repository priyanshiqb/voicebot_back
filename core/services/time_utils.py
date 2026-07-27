"""Tiny indirection over the current time so services stay easy to test."""

from django.utils import timezone


def utcnow():
    return timezone.now()
