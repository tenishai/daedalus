"""The image carrier on Message: optional, default None, round-trips."""

from daedalus.task import ImagePart, Message, Task


def test_message_images_default_none_keeps_old_call_sites_working():
    message = Message(role="user", text="hi")
    assert message.images is None
    # Serialization keeps the field (schema-stable for checkpoints), value None.
    assert message.model_dump()["images"] is None


def test_image_part_round_trips_through_model_dump():
    part = ImagePart(data="aGVsbG8=", media_type="image/png")
    message = Message(role="user", text="look", images=[part])

    dumped = message.model_dump()
    revived = Message.model_validate(dumped)

    assert revived.images is not None
    assert revived.images[0].data == "aGVsbG8="
    assert revived.images[0].media_type == "image/png"


def test_from_text_carries_no_images():
    task = Task.from_text("hello")
    assert task.history[0].images is None
