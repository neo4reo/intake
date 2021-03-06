# -*- coding: utf-8 -*-
# Generated by Django 1.10 on 2017-06-15 20:04
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('user_accounts', '0023_organization_fax_number'),
        ('intake', '0058_replace_event_logic_with_flags'),
    ]

    operations = [
        migrations.CreateModel(
            name='PrebuiltPDFBundle',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('updated', models.DateTimeField(auto_now=True, null=True)),
                ('pdf', models.FileField(upload_to='prebuilt_newapps_pdfs/')),
                ('applications', models.ManyToManyField(related_name='prebuilt_multiapp_pdfs', to='intake.Application')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='prebuilt_pdf_bundles', to='user_accounts.Organization')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
