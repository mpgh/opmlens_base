# Generated by Django 2.1.4 on 2019-01-21 17:47

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tom_reduced_data', '0003_auto_20190117_2248'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='reduceddatum',
            name='group',
        ),
        migrations.AddField(
            model_name='reduceddatum',
            name='group',
            field=models.ForeignKey(default=1, on_delete=django.db.models.deletion.CASCADE, to='tom_reduced_data.ReducedDataGrouping'),
            preserve_default=False,
        ),
    ]
