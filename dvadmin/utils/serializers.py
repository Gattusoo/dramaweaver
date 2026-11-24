"""Serializer 薄基类。"""
from rest_framework import serializers


class CustomModelSerializer(serializers.ModelSerializer):
    """业务 Serializer 的统一基类（后续可挂统一字段处理逻辑）。"""
