from pathlib import Path
from typing import Optional
from models.schemas import Slot, validate_slot_id
from models.paths import output_path
from skills.image_prompt_builder import image_prompt_skill

class VisualAgent:
    """
    Visual Agent:
    - Generates image specifications and renders branded graphic cards (1200x675)
      exclusively for accepted pieces.
    - Saves image file and metadata specification to run output directory.
    """

    def generate_visual(self, slot: Slot, output_dir: Path) -> Slot:
        # Only accepted pieces get images
        if slot.status != "accepted":
            return slot

        validate_slot_id(slot.id)
        img_file = output_path(output_dir, "images", f"{slot.id}.png")
        img_file.parent.mkdir(parents=True, exist_ok=True)

        # Build visual specification
        spec = image_prompt_skill.build_spec(slot)
        image_prompt_skill.validate_spec_alignment(slot, spec)
        
        # Render branded high-res card
        image_prompt_skill.render_card_image(spec, img_file)

        slot.image_prompt = spec.image_prompt
        slot.image_path = str(img_file.resolve())
        slot.image_spec = spec.model_dump()
        return slot

visual_agent = VisualAgent()
