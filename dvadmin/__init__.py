"""dvadmin/utils：基类与横切能力。

- models.CoreModel：抽象基类，统一 created_at / updated_at（新模型可继承；现有模型字段不动）
- viewset.CustomModelViewSet / serializers.CustomModelSerializer：薄基类
- renderers / pagination / exception：统一响应格式（design D3/D4）
- middleware：request_id 中间件
- media / storage / url_safety：创作与分发共用的横切工具
"""
