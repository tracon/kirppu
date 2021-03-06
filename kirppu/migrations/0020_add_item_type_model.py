# -*- coding: utf-8 -*-
# Generated by Django 1.11.14 on 2018-07-03 08:07
from __future__ import unicode_literals

from collections import OrderedDict

from django.db import migrations, models
import django.db.models.deletion


# noinspection SpellCheckingInspection
ITEM_TYPE = OrderedDict((
    ("manga-finnish", u"Finnish manga book"),
    ("manga-english", u"English manga book"),
    ("manga-other", u"Manga book in another language"),
    ("book", u"Non-manga book"),
    ("magazine", u"Magazine"),
    ("movie-tv", u"Movie or TV-series"),
    ("game", u"Game"),
    ("figurine-plushie", u"Figurine or a stuffed toy"),
    ("clothing", u"Clothing"),
    ("other", u"Other item"),
))


# noinspection PyPep8Naming
def migrate_item_types(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    Item = apps.get_model("kirppu", "Item")
    ItemType = apps.get_model("kirppu", "ItemType")

    if Item.objects.using(db_alias).count() == 0:
        # Don't bother creating migrated values for empty database.
        return

    updates = {}
    types = []
    for old in ITEM_TYPE.keys():
        t = ItemType(key=old, title=ITEM_TYPE[old])
        types.append(t)
        t.save(using=db_alias)  # Do not try to bulk-create these. The updated item is needed later.
        updates[old] = t

    for old_key, new_key in updates.items():
        Item.objects.using(db_alias).filter(old_itemtype=old_key).update(itemtype=new_key)


class Migration(migrations.Migration):

    dependencies = [
        ('kirppu', '0019_auto_20180703_1153'),
    ]

    operations = [
        migrations.CreateModel(
            name='ItemType',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=24, unique=True)),
                ('title', models.CharField(max_length=255)),
            ],
        ),
        migrations.RenameField(
            model_name='Item',
            old_name='itemtype',
            new_name='old_itemtype',
        ),
        migrations.AddField(
            model_name='item',
            name='itemtype',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='kirppu.ItemType'),
        ),
        migrations.RunPython(migrate_item_types),
        migrations.RemoveField(
            model_name='item',
            name='old_itemtype',
        ),
        migrations.AlterField(
            model_name='item',
            name='itemtype',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='kirppu.ItemType'),
        ),
    ]
