/* Traductions des sons YAMNet courants (le nom anglais reste la reference
 * enregistree et s'affiche en infobulle). Utilisees par CT.soundLabel. */
(function () {
    'use strict';
    var CT = window.CT = window.CT || {};
    CT.SOUNDS_FR = {
        // Voix et corps
        'Speech': 'Parole', 'Child speech, kid speaking': "Parole d'enfant", 'Conversation': 'Conversation',
        'Narration, monologue': 'Narration', 'Babbling': 'Babillage', 'Speech synthesizer': 'Voix de synthèse',
        'Shout': 'Cri', 'Yell': 'Hurlement', 'Children shouting': "Cris d'enfants", 'Screaming': 'Hurlements',
        'Whispering': 'Chuchotement', 'Laughter': 'Rire', 'Baby laughter': 'Rire de bébé', 'Giggle': 'Gloussement',
        'Chuckle, chortle': 'Petit rire', 'Crying, sobbing': 'Pleurs', 'Baby cry, infant cry': 'Pleurs de bébé',
        'Sigh': 'Soupir', 'Singing': 'Chant', 'Choir': 'Chœur', 'Child singing': "Chant d'enfant",
        'Humming': 'Fredonnement', 'Whistling': 'Sifflement', 'Breathing': 'Respiration', 'Snoring': 'Ronflement',
        'Cough': 'Toux', 'Throat clearing': 'Raclement de gorge', 'Sneeze': 'Éternuement', 'Sniff': 'Reniflement',
        'Run': 'Course', 'Walk, footsteps': 'Pas', 'Chewing, mastication': 'Mastication', 'Hiccup': 'Hoquet',
        'Burping, eructation': 'Rot',
        // Claps et foule
        'Hands': 'Mains', 'Finger snapping': 'Claquement de doigts', 'Clapping': 'Applaudissement',
        'Cheering': 'Acclamations', 'Applause': 'Applaudissements', 'Chatter': 'Bavardage', 'Crowd': 'Foule',
        'Hubbub, speech noise, speech babble': 'Brouhaha', 'Children playing': 'Enfants qui jouent',
        'Heart sounds, heartbeat': 'Battements de cœur',
        // Animaux
        'Animal': 'Animal', 'Domestic animals, pets': 'Animaux domestiques', 'Dog': 'Chien', 'Bark': 'Aboiement',
        'Howl': 'Hurlement (chien)', 'Growling': 'Grognement', 'Cat': 'Chat', 'Purr': 'Ronronnement',
        'Meow': 'Miaulement', 'Hiss': 'Sifflement (chat)', 'Horse': 'Cheval', 'Chicken, rooster': 'Poule, coq',
        'Crowing, cock-a-doodle-doo': 'Chant du coq', 'Duck': 'Canard', 'Bird': 'Oiseau',
        'Bird vocalization, bird call, bird song': "Chant d'oiseau", 'Chirp, tweet': 'Gazouillis',
        'Pigeon, dove': 'Pigeon', 'Crow': 'Corbeau', 'Owl': 'Hibou', 'Insect': 'Insecte', 'Cricket': 'Grillon',
        'Mosquito': 'Moustique', 'Fly, housefly': 'Mouche', 'Buzz': 'Bourdonnement', 'Bee, wasp, etc.': 'Abeille, guêpe',
        'Frog': 'Grenouille', 'Mouse': 'Souris',
        // Musique
        'Music': 'Musique', 'Musical instrument': 'Instrument de musique', 'Guitar': 'Guitare', 'Piano': 'Piano',
        'Drum': 'Tambour', 'Drum kit': 'Batterie', 'Percussion': 'Percussions', 'Bell': 'Cloche',
        'Church bell': "Cloche d'église", 'Jingle bell': 'Grelot', 'Chime': 'Carillon', 'Wind chime': 'Carillon à vent',
        'Violin, fiddle': 'Violon', 'Flute': 'Flûte', 'Pop music': 'Musique pop', 'Rock music': 'Rock',
        'Electronic music': 'Musique électronique', 'Classical music': 'Musique classique', 'Song': 'Chanson',
        'Background music': "Musique d'ambiance", 'Tambourine': 'Tambourin', 'Cymbal': 'Cymbale',
        // Nature
        'Wind': 'Vent', 'Thunderstorm': 'Orage', 'Thunder': 'Tonnerre', 'Water': 'Eau', 'Rain': 'Pluie',
        'Raindrop': 'Goutte de pluie', 'Rain on surface': 'Pluie sur une surface', 'Stream': 'Ruisseau',
        'Ocean': 'Océan', 'Waves, surf': 'Vagues', 'Fire': 'Feu', 'Crackle': 'Crépitement',
        // Véhicules
        'Vehicle': 'Véhicule', 'Car': 'Voiture', 'Vehicle horn, car horn, honking': 'Klaxon',
        'Car alarm': 'Alarme de voiture', 'Car passing by': 'Voiture qui passe', 'Truck': 'Camion', 'Bus': 'Bus',
        'Emergency vehicle': "Véhicule d'urgence", 'Police car (siren)': 'Sirène de police',
        'Ambulance (siren)': "Sirène d'ambulance", 'Fire engine, fire truck (siren)': 'Sirène de pompiers',
        'Motorcycle': 'Moto', 'Traffic noise, roadway noise': 'Circulation', 'Train': 'Train',
        'Aircraft': 'Avion', 'Helicopter': 'Hélicoptère', 'Bicycle': 'Vélo', 'Engine': 'Moteur',
        'Lawn mower': 'Tondeuse', 'Chainsaw': 'Tronçonneuse', 'Idling': 'Moteur au ralenti',
        // Maison
        'Door': 'Porte', 'Doorbell': 'Sonnette', 'Ding-dong': 'Ding-dong', 'Sliding door': 'Porte coulissante',
        'Slam': 'Claquement de porte', 'Knock': 'Toc (porte)', 'Tap': 'Tapotement', 'Squeak': 'Grincement',
        'Cupboard open or close': 'Placard', 'Drawer open or close': 'Tiroir',
        'Dishes, pots, and pans': 'Vaisselle', 'Cutlery, silverware': 'Couverts', 'Chopping (food)': 'Hachage',
        'Frying (food)': 'Friture', 'Microwave oven': 'Micro-ondes', 'Blender': 'Mixeur',
        'Water tap, faucet': 'Robinet', 'Sink (filling or washing)': 'Évier', 'Bathtub (filling or washing)': 'Baignoire',
        'Hair dryer': 'Sèche-cheveux', 'Toilet flush': "Chasse d'eau", 'Toothbrush': 'Brosse à dents',
        'Electric toothbrush': 'Brosse à dents électrique', 'Vacuum cleaner': 'Aspirateur', 'Zipper (clothing)': 'Fermeture éclair',
        'Keys jangling': 'Cliquetis de clés', 'Coin (dropping)': 'Pièce qui tombe', 'Scissors': 'Ciseaux',
        'Electric shaver, electric razor': 'Rasoir électrique', 'Typing': 'Frappe au clavier',
        'Computer keyboard': "Clavier d'ordinateur", 'Writing': 'Écriture',
        // Alarmes et signaux
        'Alarm': 'Alarme', 'Telephone': 'Téléphone', 'Telephone bell ringing': 'Sonnerie de téléphone',
        'Ringtone': 'Sonnerie', 'Telephone dialing, DTMF': 'Numérotation téléphonique', 'Dial tone': 'Tonalité',
        'Busy signal': 'Tonalité occupée', 'Alarm clock': 'Réveil', 'Siren': 'Sirène', 'Buzzer': 'Buzzer',
        'Smoke detector, smoke alarm': 'Détecteur de fumée', 'Fire alarm': 'Alarme incendie', 'Whistle': 'Sifflet',
        'Beep, bleep': 'Bip', 'Ping': 'Ping', 'Ding': 'Ding',
        // Mécanismes et outils
        'Mechanisms': 'Mécanisme', 'Clock': 'Horloge', 'Tick': 'Tic', 'Tick-tock': 'Tic-tac',
        'Sewing machine': 'Machine à coudre', 'Mechanical fan': 'Ventilateur', 'Air conditioning': 'Climatisation',
        'Printer': 'Imprimante', 'Camera': 'Appareil photo', 'Tools': 'Outils', 'Hammer': 'Marteau',
        'Sawing': 'Sciage', 'Power tool': 'Outil électrique', 'Drill': 'Perceuse',
        // Chocs et bruits
        'Explosion': 'Explosion', 'Gunshot, gunfire': 'Coup de feu', 'Fireworks': "Feu d'artifice",
        'Firecracker': 'Pétard', 'Burst, pop': 'Éclatement', 'Boom': 'Boum', 'Wood': 'Bois', 'Crack': 'Craquement',
        'Glass': 'Verre', 'Chink, clink': 'Tintement', 'Shatter': 'Bris de verre', 'Liquid': 'Liquide',
        'Splash, splatter': 'Éclaboussure', 'Drip': 'Goutte', 'Pour': 'Versement', 'Boiling': 'Ébullition',
        'Whoosh, swoosh, swish': 'Souffle', 'Thump, thud': 'Bruit sourd', 'Thunk': 'Choc sourd', 'Bang': 'Bang',
        'Slap, smack': 'Claque', 'Whack, thwack': 'Coup sec', 'Smash, crash': 'Fracas', 'Breaking': 'Casse',
        'Bouncing': 'Rebond', 'Scratch': 'Grattement', 'Scrape': 'Raclement', 'Rub': 'Frottement',
        'Crumpling, crinkling': 'Froissement', 'Tearing': 'Déchirement', 'Clang': 'Bruit métallique',
        'Squeal': 'Crissement', 'Creak': 'Craquement (bois)', 'Rustle': 'Bruissement', 'Whir': 'Vrombissement',
        'Clatter': 'Cliquetis', 'Sizzle': 'Grésillement', 'Clicking': 'Clic', 'Rumble': 'Grondement',
        'Jingle, tinkle': 'Tintement léger', 'Hum': 'Ronronnement électrique', 'Crunch': 'Craquement sec',
        // Ambiances
        'Silence': 'Silence', 'Sine wave': 'Sinusoïde', 'Sound effect': 'Effet sonore', 'Pulse': 'Impulsion',
        'Inside, small room': 'Intérieur, petite pièce', 'Inside, large room or hall': 'Intérieur, grande salle',
        'Inside, public space': 'Intérieur, lieu public', 'Outside, urban or manmade': 'Extérieur, ville',
        'Outside, rural or natural': 'Extérieur, nature', 'Reverberation': 'Réverbération', 'Echo': 'Écho',
        'Noise': 'Bruit', 'Environmental noise': 'Bruit ambiant', 'Static': 'Grésillement radio',
        'Mains hum': 'Ronflement secteur', 'Distortion': 'Distorsion', 'White noise': 'Bruit blanc',
        'Pink noise': 'Bruit rose', 'Vibration': 'Vibration', 'Television': 'Télévision', 'Radio': 'Radio'
    };
})();
